import asyncio
from unittest import mock

import aiohttp
import pytest

import app as app_module


def _sync(coro):
    return asyncio.run(coro)


class _FakeReqInfo:
    real_url = "https://api.openai.com/v1/audio/transcriptions"


class _Resp:
    def __init__(self, status, text="ok", headers=None):
        self.status = status
        # If `text` is bytes/str and not JSON, wrap it as verbose_json so the
        # caller's `json.loads` succeeds — tests use `text` as a content flag.
        if isinstance(text, str) and text in ("ok", "transcript"):
            self._text = '{"text": "%s", "segments": []}' % text
        else:
            self._text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=_FakeReqInfo(), history=None,
                status=self.status, message=self._text,
            )

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode()


class _FakeSession:
    """Replays a scripted sequence of responses, one per request."""
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, data=None):
        i = min(self._calls, len(self._responses) - 1)
        r = self._responses[i]
        self._calls += 1
        return r

    @property
    def calls(self):
        return self._calls


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(app_module.asyncio, "sleep", lambda s: sleeps.append(s))

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    return sleeps


def test_retries_on_429_then_succeeds(tmp_path):
    chunk = tmp_path / "c.mp3"
    chunk.write_bytes(b"x")

    responses = [_Resp(429, "rate"), _Resp(200, "transcript")]
    session = _FakeSession(responses)

    with mock.patch.object(aiohttp, "ClientSession", lambda: session):
        result = _sync(app_module.async_transcript(str(chunk), "en"))

    assert result == {"text": "transcript", "segments": []}
    assert session.calls == 2  # retried once


def test_retries_on_502_then_succeeds(tmp_path):
    chunk = tmp_path / "c.mp3"
    chunk.write_bytes(b"x")

    responses = [_Resp(502), _Resp(503), _Resp(200, "ok")]
    session = _FakeSession(responses)

    with mock.patch.object(aiohttp, "ClientSession", lambda: session):
        result = _sync(app_module.async_transcript(str(chunk), "en"))

    assert result == {"text": "ok", "segments": []}
    assert session.calls == 3


def test_honours_retry_after_header(tmp_path, monkeypatch):
    chunk = tmp_path / "c.mp3"
    chunk.write_bytes(b"x")
    monkeypatch.setattr(app_module, "RETRY_BASE_DELAY", 0)

    responses = [_Resp(429, headers={"Retry-After": "5"}), _Resp(200, "ok")]
    session = _FakeSession(responses)
    sleeps = []

    async def capture_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(app_module.asyncio, "sleep", capture_sleep)

    with mock.patch.object(aiohttp, "ClientSession", lambda: session):
        _sync(app_module.async_transcript(str(chunk), "en"))

    # Should have waited >= 5s (Retry-After) at least once.
    assert any(s >= 5 for s in sleeps)


def test_gives_up_after_max_retries(tmp_path, monkeypatch):
    chunk = tmp_path / "c.mp3"
    chunk.write_bytes(b"x")
    monkeypatch.setattr(app_module, "RETRY_BASE_DELAY", 0)

    responses = [_Resp(429) for _ in range(10)]
    session = _FakeSession(responses)

    with mock.patch.object(aiohttp, "ClientSession", lambda: session):
        with pytest.raises(aiohttp.ClientError):
            _sync(app_module.async_transcript(str(chunk), "en"))

    assert session.calls == app_module.MAX_RETRIES + 1


def test_no_retry_on_4xx_other_than_429(tmp_path):
    chunk = tmp_path / "c.mp3"
    chunk.write_bytes(b"x")

    responses = [_Resp(400, "bad")]
    session = _FakeSession(responses)

    with mock.patch.object(aiohttp, "ClientSession", lambda: session):
        with pytest.raises(aiohttp.ClientError):
            _sync(app_module.async_transcript(str(chunk), "en"))

    assert session.calls == 1  # no retries
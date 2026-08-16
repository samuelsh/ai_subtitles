import asyncio
import pathlib
from unittest import mock

import aiohttp
import pytest

import app as app_module


class _FakeReqInfo:
    real_url = "https://api.openai.com/v1/audio/transcriptions"


@pytest.fixture()
def chunk_file(tmp_path):
    p = tmp_path / "tmp_0.mp3"
    p.write_bytes(b"pretend-audio")
    return str(p)


async def _run(coro):
    return await coro


def _sync(coro):
    return asyncio.run(coro)


def test_async_transcript_posts_correct_form_fields(chunk_file):
    captured = {}

    class FakeResponse:
        status = 200
        headers = {}

        def raise_for_status(self):
            pass

        async def text(self):
            return '{"text": "ok", "segments": []}'

    class FakeSession:
        def __init__(self):
            self._entered = False

        async def __aenter__(self):
            self._entered = True
            return self

        async def __aexit__(self, *a):
            self._entered = False

        async def post(self, url, headers=None, data=None):
            captured["url"] = url
            captured["headers"] = headers
            # FormData stores parts as tuples of (headers_multidict, _, value).
            fields = {}
            for part in data._fields:
                headers_md, _params, value = part
                name = headers_md.get("name")
                if "filename" in headers_md:
                    fields["filename"] = headers_md["filename"]
                if hasattr(value, "read"):
                    value = value.read()
                fields[name] = value
            captured["fields"] = fields
            return FakeResponse()

    with mock.patch.object(aiohttp, "ClientSession", FakeSession):
        result = _sync(app_module.async_transcript(chunk_file, "en"))

    assert result == {"text": "ok", "segments": []}
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    fields = captured["fields"]
    assert fields["model"] == "whisper-1"
    assert fields["language"] == "en"
    assert fields["response_format"] == "verbose_json"
    assert fields["file"] == b"pretend-audio"
    assert pathlib.Path(chunk_file).name == fields.get("filename") or "tmp_0.mp3"


def test_async_transcript_raises_on_http_error(chunk_file):
    class FakeResponse:
        status = 400
        headers = {}

        def raise_for_status(self):
            raise aiohttp.ClientResponseError(
                request_info=_FakeReqInfo(), history=None, status=400, message="bad"
            )

        async def text(self):
            return "error body"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return FakeResponse()

    with mock.patch.object(aiohttp, "ClientSession", FakeSession):
        with pytest.raises(aiohttp.ClientError):
            _sync(app_module.async_transcript(chunk_file, "en"))


def test_async_transcript_reads_file_synchronously_not_aiofiles(chunk_file):
    """Regression: previously an aiofiles handle was passed to aiohttp, which
    could not read it. Ensure async_transcript opens the path with the builtin
    `open`, not `aiofiles.open`."""
    real_open_was_used = {"hit": False}

    real_open = open

    def tracking_open(path, *a, **kw):
        if "rb" in str(a) or kw.get("mode") == "rb":
            real_open_was_used["hit"] = True
            return real_open(path, *a, **kw)
        return real_open(path, *a, **kw)

    class FakeResponse:
        status = 200
        headers = {}

        def raise_for_status(self):
            return None

        async def text(self):
            return '{"text": "ok", "segments": []}'

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return FakeResponse()

    with (
        mock.patch("builtins.open", tracking_open),
        mock.patch.object(aiohttp, "ClientSession", FakeSession),
    ):
        _sync(app_module.async_transcript(chunk_file, "en"))
    assert real_open_was_used["hit"], "async_transcript must use builtin open()"
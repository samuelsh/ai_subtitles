import io
import json
import time
from unittest import mock

import app as app_module
from app import jobs


def _post_sync(client, name="a.mp3", fmt="text"):
    parsed = {"text": "ok", "language": "en", "duration": 1.0,
              "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]}
    async def quick(path, lang):
        return parsed
    with mock.patch.object(app_module, "async_transcript", new=quick):
        r = client.post(
            "/transcribe",
            data={"audio_file": (io.BytesIO(b"x"), name), "format": fmt},
            content_type="multipart/form-data",
        )
    return r


def _wait_terminal(job_id, timeout=5.0):
    job = jobs.get(job_id)
    if job is None:
        return None
    deadline = time.monotonic() + timeout
    while job.status == "running" and time.monotonic() < deadline:
        job.wait_for_change(job.version, 0.1)
    return job


def _parse_sse(body_text):
    """Group an SSE body into a list of (event, data) tuples."""
    events = []
    cur_event = "message"
    cur_data = []
    for line in body_text.split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):]
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "":
            if cur_data:
                events.append((cur_event, "\n".join(cur_data)))
            cur_event = "message"
            cur_data = []
    return events


class TestProgressUnknownJob:
    def test_unknown_job_returns_error_event(self, client):
        r = client.get("/progress?job_id=does-not-exist")
        body = r.get_data(as_text=True)
        assert "event: error" in body
        assert "unknown job_id" in body


class TestProgressLiveJob:
    def test_sse_emits_snapshot_then_done(self, client):
        r = _post_sync(client)
        assert r.status_code == 202
        job_id = r.get_json()["job_id"]
        _wait_terminal(job_id)
        # The /progress stream arrives once and finishes once terminal.
        r2 = client.get("/progress?job_id=" + job_id)
        body = r2.get_data(as_text=True)
        evts = _parse_sse(body)
        events_named = [e for e, _ in evts]
        # We always get at least one snapshot, then a done.
        assert events_named.count("snapshot") >= 1
        assert "done" in events_named
        # Final snapshot reflects completion.
        all_snapshots = [
            json.loads(d) for e, d in evts if e == "snapshot"
        ]
        last = all_snapshots[-1]
        assert last["status"] == "done"
        assert last["completed"] == last["total_chunks"]
        assert last["failed"] == 0

    def test_sse_emits_error_event_for_failed_job(self, client, monkeypatch):
        import aiohttp

        class Boom(aiohttp.ClientError):
            pass

        async def boom(path, lang):
            raise Boom("nope")

        with mock.patch.object(app_module, "async_transcript", new=boom):
            r = client.post(
                "/transcribe",
                data={"audio_file": (io.BytesIO(b"x"), "a.mp3")},
                content_type="multipart/form-data",
            )
        assert r.status_code == 202
        job_id = r.get_json()["job_id"]
        _wait_terminal(job_id)
        r2 = client.get("/progress?job_id=" + job_id)
        body = r2.get_data(as_text=True)
        assert "event: error" in body
        assert "Error processing audio" in body

    def test_progress_format_reflects_job_state(self, client):
        r = _post_sync(client, fmt="srt")
        job_id = r.get_json()["job_id"]
        _wait_terminal(job_id)
        r2 = client.get("/progress?job_id=" + job_id)
        body = r2.get_data(as_text=True)
        evts = _parse_sse(body)
        snapshots = [json.loads(d) for e, d in evts if e == "snapshot"]
        assert snapshots[-1]["format"] == "srt"
        assert snapshots[-1]["source_filename"] == "a.mp3"


class TestStreamingHeaders:
    def test_sse_response_has_correct_mimetype(self, client):
        r = _post_sync(client)
        job_id = r.get_json()["job_id"]
        _wait_terminal(job_id)
        r2 = client.get("/progress?job_id=" + job_id)
        assert r2.mimetype == "text/event-stream"
        assert r2.headers.get("Cache-Control") == "no-cache"
        assert r2.headers.get("X-Accel-Buffering") == "no"
        assert r2.headers.get("Connection") == "keep-alive"
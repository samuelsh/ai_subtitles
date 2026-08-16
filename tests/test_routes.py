import io
import pathlib
from unittest import mock

import aiohttp

import app as app_module


def _upload(name, content=b"id3tag", fmt="text", language="en"):
    return {
        "audio_file": (io.BytesIO(content), name),
        "format": fmt,
        "language": language,
    }


def _parsed(text="hello world", segments=None):
    return {"text": text, "language": "en", "duration": 1.0,
            "segments": segments or [{"start": 0.0, "end": 1.0, "text": text}]}


def _post_and_fetch(client, wait_for_job, *args, **kwargs):
    """POST /transcribe, wait for the job to finish, then GET /result/<id>.

    Returns (post_response, result_response).
    """
    r = client.post("/transcribe", *args, **kwargs)
    if r.status_code != 202:
        return r, None
    job_id = r.get_json()["job_id"]
    job = wait_for_job(job_id)
    assert job is not None, "job vanished before completion"
    rr = client.get("/result/" + job_id)
    return r, rr


class FakeAudio:
    """Stand-in for pydub.AudioSegment with the surface this app uses."""

    def __init__(self, duration_ms):
        self._duration = duration_ms

    def __len__(self):
        return self._duration

    def __getitem__(self, _slice):
        return self

    def export(self, path, format=None):
        return None


class FakeVideo:
    def __init__(self, audio=None):
        self.audio = audio

    def close(self):
        pass


class TestTranscribeValidation:
    def test_no_file(self, client):
        r = client.post("/transcribe", data={}, content_type="multipart/form-data")
        assert r.status_code == 400
        assert b"No audio file" in r.data

    def test_invalid_extension(self, client):
        r = client.post(
            "/transcribe", data=_upload("movie.mov"), content_type="multipart/form-data"
        )
        assert r.status_code == 400
        assert b"Invalid audio file format" in r.data

    def test_empty_filename(self, client):
        r = client.post(
            "/transcribe", data=_upload(""), content_type="multipart/form-data"
        )
        assert r.status_code == 400


class TestSmallFileTranscription:
    def test_small_mp3_returns_text(self, client, wait_for_job):
        with mock.patch.object(
            app_module, "async_transcript", new=mock.AsyncMock(return_value=_parsed("hello world"))
        ):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("song.mp3", content=b"audio-bytes"),
                content_type="multipart/form-data",
            )
        assert post_r.status_code == 202
        assert r.status_code == 200
        assert r.data == b"hello world"
        assert r.headers["Content-Disposition"].endswith(".txt")

    def test_srt_format_extension_and_sentence_split(self, client, wait_for_job):
        parsed = _parsed("Sentence one. Sentence two.", [{"start": 0.0, "end": 10.0, "text": "Sentence one. Sentence two."}])
        with mock.patch.object(
            app_module, "async_transcript", new=mock.AsyncMock(return_value=parsed)
        ):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("song.mp3", fmt="srt"),
                content_type="multipart/form-data",
            )
        assert post_r.status_code == 202
        assert r.status_code == 200
        assert r.headers["Content-Disposition"].endswith(".srt")
        body = r.data.decode()
        # One segment, two sentences -> two sequentially numbered SRT cues.
        assert "1\n00:00:00,000" in body
        assert "2\n" in body
        assert "Sentence one." in body and "Sentence two." in body


class TestMp4Conversion:
    def test_mp4_with_no_audio_track(self, client):
        with mock.patch.object(
            app_module, "VideoFileClip", return_value=FakeVideo(audio=None)
        ):
            r = client.post(
                "/transcribe",
                data=_upload("clip.mp4"),
                content_type="multipart/form-data",
            )
        assert r.status_code == 400
        assert b"no audio track" in r.data

    def test_mp4_with_audio_succeeds(self, client, wait_for_job):
        fake_audio = mock.MagicMock()
        write_calls = []

        def fake_write(path):
            write_calls.append(path)
            pathlib.Path(path).write_bytes(b"converted")
            return None

        fake_audio.write_audiofile = fake_write
        with (
            mock.patch.object(app_module, "VideoFileClip", return_value=FakeVideo(audio=fake_audio)),
            mock.patch.object(
                app_module, "async_transcript", new=mock.AsyncMock(return_value=_parsed("from video"))
            ),
        ):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("clip.mp4"),
                content_type="multipart/form-data",
            )
        assert post_r.status_code == 202
        assert r.status_code == 200
        assert r.data == b"from video"
        assert len(write_calls) == 1


class TestChunking:
    def test_long_file_chunked_and_joined_in_order(self, client, monkeypatch, wait_for_job):
        # Force the chunking path for any non-empty file.
        monkeypatch.setattr(app_module, "WHISPER_MAX_BYTES", 0)
        # 25-minute audio -> 3 chunks (the old code dropped the last one).
        fake_audioseg = mock.Mock()
        fake_audioseg.from_file = mock.Mock(return_value=FakeAudio(25 * 60 * 1000))
        monkeypatch.setattr(app_module, "AudioSegment", fake_audioseg)

        async def fake_transcript(path, lang):
            # Encode the offset from the temp filename so we can verify order.
            offset = int(path.rsplit("_", 1)[-1].split(".")[0])
            text = f"chunk{offset}"
            seg = {"id": offset, "start": 0.0, "end": 60.0, "text": text}
            return {"text": text, "language": "en", "duration": 60.0, "segments": [seg]}

        # Render as text -> the merged "text" field concatenates per chunk.
        with mock.patch.object(app_module, "async_transcript", new=fake_transcript):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("big.mp3", content=b"x", fmt="text"),
                content_type="multipart/form-data",
            )
        assert post_r.status_code == 202
        assert r.status_code == 200
        # Concatenated text in offset order, not HTTP completion order.
        assert r.data == b"chunk0chunk1chunk2"

    def test_chunked_srt_has_absolute_timestamps_and_unique_indices(self, client, monkeypatch, wait_for_job):
        monkeypatch.setattr(app_module, "WHISPER_MAX_BYTES", 0)
        monkeypatch.setattr(app_module, "AudioSegment", mock.Mock(
            from_file=mock.Mock(return_value=FakeAudio(20 * 60 * 1000))
        ))

        async def fake_transcript(path, lang):
            offset = int(path.rsplit("_", 1)[-1].split(".")[0])
            # 6s segment at 0:00 of each chunk (relative). After offset:
            # chunk0 -> 0..6, chunk1 -> 600..606.
            text = f"chunk{offset} sentence."
            return {"text": text, "segments": [{"start": 0.0, "end": 6.0, "text": text}]}

        with mock.patch.object(app_module, "async_transcript", new=fake_transcript):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("big.mp3", content=b"x", fmt="srt"),
                content_type="multipart/form-data",
            )
        body = r.data.decode()
        assert post_r.status_code == 202
        assert r.status_code == 200
        # Chunk 0's cue uses 00:00..00:06.
        assert "00:00:00,000 --> 00:00:06,000" in body
        # Chunk 1's cue uses 10:00..10:06 — absolute time, not chunk-relative
        # (the old code would output 00:00:00,000 for every chunk).
        assert "00:10:00,000 --> 00:10:06,000" in body
        # Indices are sequential across chunks (NOT restarted at 1 per chunk).
        assert body.startswith("1\n")
        assert "\n2\n" in body
        assert "\n3\n" not in body  # only 2 chunks -> 2 cues

    def test_chunk_failure_returns_502_not_corrupted_output(self, client, monkeypatch, wait_for_job):
        monkeypatch.setattr(app_module, "WHISPER_MAX_BYTES", 0)
        monkeypatch.setattr(
            app_module, "AudioSegment", mock.Mock(
                from_file=mock.Mock(return_value=FakeAudio(20 * 60 * 1000))
            )
        )

        class Boom(aiohttp.ClientError):
            pass

        async def boom(path, lang):
            raise Boom("nope")

        with mock.patch.object(app_module, "async_transcript", new=boom):
            post_r, r = _post_and_fetch(client, wait_for_job,
                data=_upload("big.mp3", content=b"x"),
                content_type="multipart/form-data",
            )
        assert post_r.status_code == 202
        # The job ends in error; /result returns 502 with the message.
        assert r.status_code == 502
        assert b"Error processing audio" in r.data


class TestJobLifecycle:
    def test_post_returns_job_id(self, client):
        with mock.patch.object(
            app_module, "async_transcript", new=mock.AsyncMock(return_value=_parsed())
        ):
            r = client.post(
                "/transcribe",
                data=_upload("song.mp3", content=b"audio"),
                content_type="multipart/form-data",
            )
        assert r.status_code == 202
        body = r.get_json()
        assert "job_id" in body
        assert len(body["job_id"]) >= 16

    def test_concurrent_post_returns_409(self, client, wait_for_job):
        # Stub async_transcript to block on an event so the first job stays
        # in "running" state while we issue the second POST.
        gate = app_module.asyncio.Event()

        async def slow_transcript(path, lang):
            await gate.wait()
            return _parsed()

        with mock.patch.object(app_module, "async_transcript", new=slow_transcript):
            r1 = client.post(
                "/transcribe",
                data=_upload("a.mp3", content=b"x"),
                content_type="multipart/form-data",
            )
            assert r1.status_code == 202
            r2 = client.post(
                "/transcribe",
                data=_upload("b.mp3", content=b"y"),
                content_type="multipart/form-data",
            )
            # Release the first job so cleanup fixtures pass.
            gate.set()
            wait_for_job(r1.get_json()["job_id"])
        assert r2.status_code == 409
        assert b"already running" in r2.data
        assert r2.get_json()["retry_after"] == 30

    def test_result_unknown_job_returns_404(self, client):
        r = client.get("/result/doesnotexist")
        assert r.status_code == 404

    def test_result_running_job_returns_409(self, client, wait_for_job):
        gate = app_module.asyncio.Event()

        async def slow_transcript(path, lang):
            await gate.wait()
            return _parsed()

        with mock.patch.object(app_module, "async_transcript", new=slow_transcript):
            post = client.post(
                "/transcribe",
                data=_upload("a.mp3", content=b"x"),
                content_type="multipart/form-data",
            )
            job_id = post.get_json()["job_id"]
            r = client.get("/result/" + job_id)
            gate.set()
            wait_for_job(job_id)
        assert r.status_code == 409
        assert b"running" in r.data

    def test_result_done_job_serves_file_and_evicts(self, client, wait_for_job):
        with mock.patch.object(
            app_module, "async_transcript", new=mock.AsyncMock(return_value=_parsed("done bytes"))
        ):
            post = client.post(
                "/transcribe",
                data=_upload("a.mp3", content=b"x"),
                content_type="multipart/form-data",
            )
            job_id = post.get_json()["job_id"]
            wait_for_job(job_id)
            r = client.get("/result/" + job_id)
            assert r.status_code == 200
            assert r.data == b"done bytes"
            # Second fetch: job is evicted after a successful download.
            r2 = client.get("/result/" + job_id)
        assert r2.status_code == 404


class TestHomeRoute:
    def test_home_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_home_advertises_2_gib_limit(self, client):
        r = client.get("/")
        assert b"2&nbsp;GiB" in r.data or b"2 GiB" in r.data

    def test_home_has_progress_container(self, client):
        r = client.get("/")
        assert b'id="progress"' in r.data
        assert b'id="progress_fill"' in r.data
        assert b'id="progress_log"' in r.data
# AI Subtitles

Subtitle generator from audio and video files using the OpenAI Whisper
audio transcription API. Upload `.mp3`, `.wav`, `.ogg`, or `.mp4`; get back a
`.txt` or `.srt` file. Long files (>25 MiB) are split into 10-minute chunks,
transcribed concurrently, and reassembled in order.

## Prerequisites

- Python 3.11+ (3.13 supported; see note below)
- `ffmpeg` on your PATH (moviepy/pydub use it for MP4 conversion and chunking)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `apt-get install ffmpeg`
- An OpenAI API key

## Run locally (development server)

```bash
# 1. Create & activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements-dev.txt

# 3. Provide your OpenAI key
export OPENAI_API_KEY=sk-...

# 4. Start the dev server (serves on http://localhost:8001)
python run.py
```

Open http://localhost:8001, pick a file, choose format & language, and submit.

## Run with Docker

```bash
export OPENAI_API_KEY=sk-...
docker compose up --build
```

The app is served on http://localhost:5000. Logs are written to
`/var/log/ai_subtitles-web` on the host (mounted to `/app/log` in the
container).

## Run tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -v
```

There are 24 tests covering: file validation, format→extension mapping,
chunk-offset math (incl. the trailing-chunk regression), MP4 audio handling,
end-to-end route behaviour with mocked transcription, and the `async_transcript`
client (form fields, error propagation, synchronous file reads).

## Configuration

All settings live in `config.py` and are overridable via environment variables:

| Variable          | Purpose                                   | Default                |
| ----------------- | ----------------------------------------- | ---------------------- |
| `OPENAI_API_KEY`  | Required. Key for the Whisper API.        | `""`                   |
| `SECRET_KEY`      | Flask session cookie signing key.         | `"dev-secret-change-me"` |
| `DEBUG`           | Flask debug mode.                         | `True`                 |
| `OPENAI_API_KEY`  | Required. Key for the Whisper API.        | `""`                   |
| `SECRET_KEY`      | Flask session cookie signing key.         | `"dev-secret-change-me"` |
| `DEBUG`           | Flask debug mode.                         | `True`                 |
| `CHUNK_CONCURRENCY`| Parallel Whisper requests per long file.  | `4`                    |
| `UPLOAD_LIMIT_BYTES`| Max upload size in bytes.                 | `2147483648` (2 GiB)   |

The app retries `429` (rate limit) and `5xx` responses up to 5 times with
exponential backoff (1s → 2s → 4s → 8s → 16s), and honours OpenAI's
`Retry-After` header when present. `CHUNK_CONCURRENCY` defaults to 4 — a
reasonable balance for Tier-1 RPM/TPM limits. Raise it for higher tiers; the
retry layer auto-throttles if you burst past OpenAI's limits.

MP4 video support is end-to-end: uploads up to 2&nbsp;GiB are accepted, the
audio track is extracted via moviepy, and the resulting MP3 is chunked and
transcribed like any other audio file. Note that video extraction and large
uploads add latency independent of OpenAI.

### Streaming progress (long jobs)

Transcription is asynchronous and reports real-time progress:

1. `POST /transcribe` accepts the multipart upload and returns `202 Accepted`
   with `{"job_id": "…"}`. The transcription itself runs in a background
   worker thread so the request returns immediately.
2. `GET /progress?job_id=…` is a Server-Sent Events stream emitting
   `snapshot` events (full job state) as chunks finish, a terminal `done`
   event on success, or `error` on failure. Heartbeats keep proxies from
   closing the idle connection.
3. `GET /result/<job_id>` serves the rendered subtitles file once the job
   is `done` (returns `409` while running, `404` for unknown/expired jobs,
   `502` if the job failed).

Only one transcription can run at a time per server; a second `POST` while
one is running returns `409` with `retry_after: 30`. Terminal jobs are
reaped 5 minutes after completion so memory stays bounded. Output bytes
live in memory until fetched (or reaped).

### Subtitle formatting

The app always requests `verbose_json` from Whisper (per-segment timing), then
**splits each segment on sentence boundaries** and emits one SRT/VTT cue per
sentence. Timestamps are interpolated proportionally to character length within
each segment. This produces finer-grained, sentence-aligned cues than Whisper's
built-in `srt`/`vtt` formatters, which lump multiple sentences per cue and
sometimes cut mid-sentence.

For files over the Whisper 25&nbsp;MiB limit, audio is split into 10-minute
chunks that are transcribed concurrently. Segment timestamps are offset to
absolute file positions before merging, and SRT/VTT cue indices are sequential
across the whole file (no duplicates, no restart at 1).

Upload size is capped at 2&nbsp;GiB (`UPLOAD_LIMIT_BYTES`, env-overridable).
This accommodates typical 720p/1080p educational videos up to ~2 hours.

## Notes

- The dev server (`run.py`) runs with `debug=True`; do not expose it to the
  network. Use Docker/gunicorn for anything shared.
- The `db` service in `docker-compose.yml` is provisioned but not used by the
  app yet — safe to remove if you don't need Postgres.
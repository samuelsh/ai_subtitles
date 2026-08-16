import functools
import pathlib
import asyncio
import aiohttp
import aiofiles
import heapq
import os
import json
import re
import time
import tempfile
import shutil
import threading

from io import BytesIO
from datetime import datetime, timedelta
import concurrent.futures

from easypy.units import MiB
from pydub import AudioSegment
from moviepy.editor import VideoFileClip
from flask import Flask, render_template, jsonify, request, send_file, stream_with_context
from werkzeug.utils import secure_filename

from app import jobs

TEN_MINUTES = 10 * 60
CHUNK_CONCURRENCY = int(os.environ.get("CHUNK_CONCURRENCY", "4"))
UPLOAD_LIMIT_BYTES = int(os.environ.get("UPLOAD_LIMIT_BYTES", 2 * 1024 * MiB))
WHISPER_MAX_BYTES = 25 * MiB

# Retry policy for transient transcription API failures (429 / 5xx).
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0

ALLOWED_EXTENSIONS = (".mp3", ".mp4", ".wav", ".ogg")

EXT_BY_RESPONSE_FORMAT = {
    "text": "txt",
    "srt": "srt",
    "vtt": "vtt",
    "json": "json",
    "verbose_json": "json",
}

app = Flask(__name__)
app.config.from_object("config")
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_LIMIT_BYTES

# Read the key at import time for use in the Authorization header. We talk to
# the Whisper REST endpoint directly via aiohttp rather than the openai SDK, so
# we don't construct an OpenAI client here.
_openai_key = (
        app.config.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
)


def allowed_file(filename):
    return bool(filename) and filename.lower().endswith(ALLOWED_EXTENSIONS)


def response_format_to_ext(fmt):
    return EXT_BY_RESPONSE_FORMAT.get(fmt, "txt")


def chunk_offsets(len_in_sec, chunk_seconds=TEN_MINUTES):
    """Indices of 10-min chunks that cover a file of `len_in_sec` seconds.

    Uses ceil so the trailing partial chunk is never dropped and no extra
    empty chunk is appended when the length is an exact multiple.
    """
    if len_in_sec <= 0 or chunk_seconds <= 0:
        return range(0)
    n = (len_in_sec + chunk_seconds - 1) // chunk_seconds
    return range(n)


# --- Subtitle rendering helpers -----------------------------------------------
#
# We always ask Whisper for `verbose_json` upstream (regardless of the user's
# chosen download format) because it gives us per-segment start/end timing.
# We then split each segment's text on sentence boundaries and emit one cue
# per sentence, with timestamps interpolated proportionally to character
# length. This produces finer-grained, sentence-aligned cues than Whisper's
# built-in `srt`/`vtt` formatters, which lump multiple sentences per cue and
# sometimes cut mid-sentence.

_SENTENCE_END = ".!?\"'"
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\Z")


def format_srt_ts(seconds):
    """00:00:00,000 — SubRip timestamp format."""
    ms = int(round(seconds * 1000))
    if ms < 0:
        ms = 0
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_ts(seconds):
    """00:00:00.000 — WebVTT timestamp format."""
    ms = int(round(seconds * 1000))
    if ms < 0:
        ms = 0
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def split_sentences(text):
    """Split text into sentences on terminators (.!?). Whitespace around
    each sentence is trimmed; empty results are discarded. Terminators are
    kept attached to their sentence."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts


def build_cues(segments, base_offset_seconds=0.0):
    """Yield (start, end, text) cues, one per sentence.

    For each Whisper segment, split its text on sentence terminators and
    distribute the segment's [start, end] time range proportionally to the
    character length of each sentence. This keeps timestamps monotonic and
    bounded by the segment's own timing, even when Whisper's segment
    boundary falls mid-sentence.
    """
    cues = []
    for seg in segments or []:
        try:
            seg_start = float(seg.get("start", 0.0)) + base_offset_seconds
            seg_end = float(seg.get("end", seg_start)) + base_offset_seconds
        except (TypeError, ValueError):
            seg_start = seg_end = base_offset_seconds
        text = (seg.get("text") or "").strip()
        sentences = split_sentences(text)
        if not sentences:
            continue
        if seg_end < seg_start:
            seg_end = seg_start
        if len(sentences) == 1:
            cues.append((seg_start, seg_end, sentences[0]))
            continue
        total = sum(len(s) for s in sentences)
        if total <= 0:
            # All-whitespace edge case: give the whole segment to the first.
            cues.append((seg_start, seg_end, sentences[0]))
            continue
        cur = seg_start
        span = seg_end - seg_start
        for i, s in enumerate(sentences):
            if i == len(sentences) - 1:
                end = seg_end
            else:
                end = cur + span * (len(s) / total)
            end = max(end, cur + 0.001)
            cues.append((cur, end, s))
            cur = end
    return cues


def render_srt(cues):
    """Render a list of (start, end, text) cues as a SubRip string."""
    out = []
    for i, (start, end, text) in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{format_srt_ts(start)} --> {format_srt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_vtt(cues):
    """Render a list of (start, end, text) cues as a WebVTT string."""
    out = ["WEBVTT", ""]
    for start, end, text in cues:
        out.append(f"{format_vtt_ts(start)} --> {format_vtt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def merge_parsed_chunks(chunk_results):
    """Merge per-chunk verbose_json dicts into a single dict.

    `chunk_results` is an iterable of (offset_seconds, parsed_dict). Timestamps
    on segments are offset by `offset_seconds` so the merged result reflects
    absolute positions in the original file. The `text` field is concatenated
    and `duration` is the max end-time across chunks.
    """
    merged = {"text": "", "language": None, "duration": 0.0, "segments": []}
    for offset, parsed in chunk_results:
        if not isinstance(parsed, dict):
            continue
        merged["text"] += parsed.get("text", "")
        if merged["language"] is None:
            merged["language"] = parsed.get("language")
        try:
            chunk_dur = float(parsed.get("duration", 0.0))
        except (TypeError, ValueError):
            chunk_dur = 0.0
        merged["duration"] = max(merged["duration"], offset + chunk_dur)
        for seg in parsed.get("segments", []) or []:
            new_seg = dict(seg)
            try:
                new_seg["start"] = float(seg.get("start", 0.0)) + offset
                new_seg["end"] = float(seg.get("end", 0.0)) + offset
            except (TypeError, ValueError):
                new_seg["start"] = offset
                new_seg["end"] = offset
            merged["segments"].append(new_seg)
    return merged


def render_subtitles(parsed, target_format):
    """Render a parsed verbose_json dict into the requested output format."""
    if target_format == "text":
        return (parsed.get("text") or "").strip()
    if target_format in ("json", "verbose_json"):
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    cues = build_cues(parsed.get("segments", []))
    if target_format == "srt":
        return render_srt(cues)
    if target_format == "vtt":
        return render_vtt(cues)
    return (parsed.get("text") or "").strip()


@app.errorhandler(413)
def too_large(error):
    return jsonify({"error": "Uploaded file exceeds size limit"}), 413


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


@app.route("/")
def home():
    return render_template("home.html")


async def async_write_audiofile(filename, audio_clip):
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        await loop.run_in_executor(pool, audio_clip.write_audiofile, filename)


async def async_transcript(path, language):
    """Send one chunk to the Whisper transcription endpoint.

    Always requests `verbose_json` upstream so we get per-segment timing,
    which lets us build finer-grained, sentence-aligned cues than Whisper's
    built-in `srt`/`vtt` formatters. Returns the parsed JSON as a dict.

    Retries with exponential backoff on 429 (rate limit) and 5xx
    (transient server) responses. Raises aiohttp.ClientError on any
    non-retryable transport/HTTP failure so callers can surface a clean
    error instead of silently joining a stringified Response into the
    output.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with open(path, "rb") as tmp_file:
                form_data = aiohttp.FormData()
                form_data.add_field(
                    "file",
                    tmp_file,
                    filename=pathlib.Path(path).name,
                    content_type="audio/mpeg",
                )
                for key, value in {
                    "model": "whisper-1",
                    "language": language,
                    "response_format": "verbose_json",
                }.items():
                    form_data.add_field(key, value)

                async with aiohttp.ClientSession() as session:
                    response = await session.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {_openai_key}"},
                        data=form_data,
                    )
                    if response.status in RETRY_STATUSES and attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = max(delay, float(retry_after))
                            except ValueError:
                                pass
                        app.logger.warning(
                            f"OpenAI returned {response.status} on "
                            f"{pathlib.Path(path).name}; retry {attempt + 1}/"
                            f"{MAX_RETRIES} after {delay:.1f}s"
                        )
                        await response.read()
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    body = await response.text()
                    return json.loads(body)
        except aiohttp.ClientError as e:
            # Only retry the kind of network blip where retrying is cheap and
            # likely to help (connection reset, timeout). HTTP-level errors
            # from `raise_for_status` for non-retryable statuses are re-raised
            # here only after the loop decides not to retry them.
            if (
                isinstance(e, aiohttp.ClientResponseError)
                and e.status not in RETRY_STATUSES
            ):
                raise
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                app.logger.warning(
                    f"Transport error for {pathlib.Path(path).name}: {e}; "
                    f"retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise aiohttp.ClientError("Transcription failed after retries")


async def _transcribe_chunk(work_dir, audio_file, time_offset, language, sem):
    async with sem:
        start = time_offset * TEN_MINUTES * 1_000
        chunk = audio_file[start : start + TEN_MINUTES * 1_000]
        chunk_path = work_dir / f"tmp_{time_offset}.mp3"
        await asyncio.to_thread(
            functools.partial(chunk.export, str(chunk_path), format="mp3")
        )
        app.logger.info(
            f"Processing tmp_{time_offset}.mp3 "
            f"start={time_offset * TEN_MINUTES}, "
            f"end={time_offset * TEN_MINUTES + TEN_MINUTES}"
        )
        try:
            parsed = await async_transcript(str(chunk_path), language)
        finally:
            chunk_path.unlink(missing_ok=True)
        # Offset seconds for this chunk's segment timestamps so the merged
        # result reflects absolute positions in the original file.
        return time_offset, time_offset * TEN_MINUTES, parsed


async def _transcribe_chunk_with_progress(
    job, work_dir, audio_file, time_offset, language, sem
):
    """Per-chunk transcription that also updates the live Job's progress.

    The job is updated under the registry's slot semantics: only the
    run_job task mutates a running job's counters, so no extra locking
    is needed here. We mark the chunk in_flight -> done/failed and wake
    any subscriber.
    """
    job.in_flight.add(time_offset)
    job.update()
    try:
        offset, offset_sec, parsed = await _transcribe_chunk(
            work_dir, audio_file, time_offset, language, sem
        )
        job.completed += 1
        return offset, offset_sec, parsed
    except Exception as e:
        job.failed += 1
        job.errors.append(f"chunk {time_offset}: {e}")
        job.update()
        raise
    finally:
        job.in_flight.discard(time_offset)
        job.update()


async def run_job(job, upload_path, path_to_audio):
    """Background coroutine that performs the actual transcription work
    for a job and writes the result (or error) back into the job entry.

    The caller (POST /transcribe) is responsible for cleaning up work_dir
    once the job reaches a terminal state via the reaper / explicit GC.
    """
    try:
        size = await asyncio.to_thread(
            lambda: pathlib.Path(path_to_audio).stat().st_size
        )

        if size >= WHISPER_MAX_BYTES:
            app.logger.info(
                f"Media file is greater than {WHISPER_MAX_BYTES}, splitting to chunks ..."
            )
            audio_file = await asyncio.to_thread(
                AudioSegment.from_file, str(path_to_audio)
            )
            len_in_sec = len(audio_file) // 1000
            app.logger.info(f"Audio file duration={timedelta(seconds=len_in_sec)}")

            offsets = list(chunk_offsets(int(len_in_sec)))
            job.total_chunks = len(offsets)
            job.update()  # wake subscribers so they see total_chunks

            sem = asyncio.Semaphore(CHUNK_CONCURRENCY)
            tasks = [
                _transcribe_chunk_with_progress(
                    job, work_dir_for(job), audio_file, offset, job.language, sem
                )
                for offset in offsets
            ]
            results = await asyncio.gather(*tasks)

            results.sort(key=lambda item: item[0])
            chunk_results = [(offset_sec, parsed) for _, offset_sec, parsed in results]
            parsed = merge_parsed_chunks(chunk_results)
        else:
            job.total_chunks = 1
            job.update()
            parsed = await async_transcript(str(path_to_audio), job.language)
            job.completed = 1
            job.update()

        subtitles_buf = render_subtitles(parsed, job.format)
        if not isinstance(subtitles_buf, str):
            subtitles_buf = str(subtitles_buf)

        job.result = subtitles_buf
        job.mark_terminal("done")
    except aiohttp.ClientError as e:
        app.logger.exception("Transcription API error")
        job.error_message = f"Error processing audio: {e}"
        job.mark_terminal("error")
    except Exception as e:
        app.logger.exception("Transcription job failed")
        job.error_message = f"Transcription failed: {e}"
        job.mark_terminal("error")


# Per-job temp directory bookkeeping. Each job's POST handler creates its
# own work_dir; we keep a small map so the run_job task can locate it for
# chunk export without threading it through every call. Cleared on terminal.
_job_work_dirs: dict[str, pathlib.Path] = {}


def work_dir_for(job) -> pathlib.Path:
    return _job_work_dirs.get(job.job_id, pathlib.Path(tempfile.gettempdir()))


@app.route("/transcribe", methods=["POST"])
async def transcribe():
    if "audio_file" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    media_file = request.files["audio_file"]
    app.logger.info(
        f"File Info: {media_file.filename=}, {media_file.content_type=}"
    )

    if not allowed_file(media_file.filename):
        return jsonify({"error": "Invalid audio file format"}), 400

    dnl_format = request.form.get("format", "text")
    language = request.form.get("language", "en")

    # Enforce single-running-job invariant.
    jobs.reap_finished()  # drop stale terminal jobs first
    if not jobs.acquire_job_slot():
        return (
            jsonify(
                {
                    "error": "A transcription job is already running. Please retry shortly.",
                    "retry_after": 30,
                }
            ),
            409,
        )

    safe_name = secure_filename(media_file.filename) or "upload"
    work_dir = pathlib.Path(tempfile.mkdtemp(prefix="subtitles_"))

    try:
        upload_path = work_dir / safe_name
        async with aiofiles.open(upload_path, "w+b") as tmp_file:
            while buf := media_file.read(1024 * 1024):
                await tmp_file.write(buf)

        path_to_audio = upload_path

        if media_file.filename.lower().endswith(".mp4"):
            app.logger.info(f"Converting {media_file.filename} to MP3")
            video = await asyncio.to_thread(VideoFileClip, str(upload_path))
            try:
                audio_clip = video.audio
                if audio_clip is None:
                    return jsonify({"error": "Video file has no audio track"}), 400
                out_audio = work_dir / "tmp_media_file.mp3"
                await async_write_audiofile(str(out_audio), audio_clip)
                path_to_audio = out_audio
                app.logger.info(
                    f"Media file {media_file.filename} was converted to mp3"
                )
            finally:
                video.close()

        # Create the job and kick off the background worker. We hold the
        # temp dir in _job_work_dirs so run_job can write chunk files into
        # it; cleanup happens in /result or via the reaper when the job
        # is fetched or expires.
        job = jobs.create_job(
            format=dnl_format,
            language=language,
            source_filename=media_file.filename,
        )
        _job_work_dirs[job.job_id] = work_dir
        work_dir = None  # ownership transferred; do not clean up below

        submit_run_job(job, upload_path, path_to_audio)

        return jsonify({"job_id": job.job_id}), 202
    except Exception:
        # If anything blew up before we handed off to run_job, clean up
        # the temp dir ourselves; otherwise it leaks until process exit.
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise


# --- Background worker -------------------------------------------------------
#
# run_job is async (it uses async_transcript, asyncio.gather, etc.) but it
# must outlive the per-request event loop that Flask's async view runs on.
# We keep a dedicated worker thread with its own long-lived event loop and
# submit run_job coroutines to it via run_coroutine_threadsafe. This keeps
# the work off the request loop so the POST handler can return 202
# immediately and the SSE handler can stay open reporting progress.

_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _ensure_worker():
    global _worker_loop, _worker_thread
    with _worker_lock:
        if _worker_loop is not None and not _worker_loop.is_closed():
            return
        _worker_loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_worker_loop)
            _worker_loop.run_forever()

        _worker_thread = threading.Thread(
            target=_run, name="transcribe-worker", daemon=True
        )
        _worker_thread.start()


def submit_run_job(job, upload_path, path_to_audio):
    """Schedule run_job on the dedicated worker loop. Returns immediately."""
    _ensure_worker()
    assert _worker_loop is not None  # _ensure_worker populated it
    fut = asyncio.run_coroutine_threadsafe(
        run_job(job, upload_path, path_to_audio), _worker_loop
    )
    # Stash the future so we can observe failures from tests / logs. We
    # don't await it here — the caller wants 202-immediate return. Drop
    # the entry when the future completes so the dict stays bounded.
    _pending_futures[job.job_id] = fut

    def _drop(_f, jid=job.job_id):
        _pending_futures.pop(jid, None)

    fut.add_done_callback(_drop)


_pending_futures: dict[str, "concurrent.futures.Future"] = {}


@app.route("/progress")
def progress():
    job_id = request.args.get("job_id", "")
    job = jobs.get(job_id)
    if job is None:
        def missing():
            yield 'event: error\ndata: {"message":"unknown job_id"}\n\n'.encode()
        return app.response_class(missing(), mimetype="text/event-stream")

    def stream():
        # Sync generator. job.wait_for_change() is a threading.Condition
        # wait tuned for cross-thread signaling; safe to call from a
        # request thread regardless of the worker thread's loop. We
        # re-snapshot on every wake and emit when state changes. Yields
        # bytes (Werkzeug's dev server asserts the iterable emits bytes,
        # not str).
        last_version = -1
        heartbeat_at = time.time()
        while True:
            snap = job.snapshot()
            if snap["version"] != last_version:
                last_version = snap["version"]
                yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n".encode()
            if job.status in ("done", "error"):
                terminal_event = "done" if job.status == "done" else "error"
                payload = dict(snap)
                if job.status == "error":
                    payload = {"message": job.error_message or "unknown error", **payload}
                yield f"event: {terminal_event}\ndata: {json.dumps(payload)}\n\n".encode()
                return
            # Wait off-thread for the next state change, with a short cap
            # so we periodically emit heartbeats for proxy friendliness.
            job.wait_for_change(last_version, 0.25)
            if time.time() - heartbeat_at >= 15:
                yield b": keep-alive\n\n"
                heartbeat_at = time.time()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return app.response_class(
        stream(), mimetype="text/event-stream", headers=headers, direct_passthrough=True
    )


@app.route("/result/<job_id>")
async def result(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job_id"}), 404
    if job.status == "running":
        return jsonify({"error": "Job still running"}), 409
    if job.status == "error":
        return jsonify({"error": job.error_message or "unknown error"}), 502
    # status == "done"
    buf = BytesIO((job.result or "").encode())
    ext = response_format_to_ext(job.format)
    filename = f"subtitles_{datetime.now().strftime('%y_%m_%d_%H%M%S')}.{ext}"
    # Free the in-memory result and temp dir once fetched.
    job.result = None
    wd = _job_work_dirs.pop(job.job_id, None)
    if wd is not None:
        shutil.rmtree(wd, ignore_errors=True)
    jobs.remove_job(job.job_id)
    return send_file(buf, as_attachment=True, download_name=filename)


# Background reaper thread: terminal jobs older than JOB_TTL_SECONDS are
# evicted so memory stays bounded regardless of whether the client ever
# fetched /result. Started lazily (and only once) via before_request.
_reaper_started = False
_reaper_lock = threading.Lock()


def _ensure_reaper():
    global _reaper_started
    with _reaper_lock:
        if _reaper_started:
            return
        _reaper_started = True
        t = threading.Thread(
            target=jobs.reaper_loop, name="job-reaper", daemon=True
        )
        t.start()


@app.before_request
def _start_background_threads():
    _ensure_reaper()
    _ensure_worker()
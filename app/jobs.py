"""In-memory registry of transcription jobs.

A job represents one uploaded media file moving through the chunked
transcription pipeline. Progress is exposed to clients via the SSE
endpoint GET /progress?job_id=..., and the rendered result is fetched
via GET /result/<job_id>.

Design constraints:
- Only one job runs at a time on this server (see acquire_job_slot()).
- Job state lives in process memory; nothing is persisted. A reaper
  task removes terminal jobs 5 minutes after they finish so memory is
  bounded regardless of how many jobs run over the lifetime of the
  process.
- Mutations go through Job.update() which is thread-safe (the worker
  thread writes, request handlers read). Waking subscribers uses a
  threading.Condition because the writer (worker thread) and the
  readers (SSE handlers) live on different event loops.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


JOB_TTL_SECONDS = 5 * 60  # terminal jobs are reaped 5 min after finishing


@dataclass
class Job:
    job_id: str
    format: str
    language: str
    source_filename: str
    created_at: float = field(default_factory=time.time)
    status: str = "running"  # running | done | error
    total_chunks: int = 0
    completed: int = 0
    failed: int = 0
    in_flight: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    finished_at: Optional[float] = None
    result: Optional[str] = None            # rendered subtitles, in memory
    error_message: Optional[str] = None
    # Wakes any SSE subscriber waiting on this job. A threading.Condition
    # is used because the writer (worker thread) and readers (request
    # handlers, each on their own asgiref event loop) are on different
    # loops; asyncio.Event is not thread-safe across loops.
    _cond: threading.Condition = field(default_factory=threading.Condition)
    # Bumped on every mutation so readers can detect "something changed"
    # without deep-comparing snapshots.
    version: int = 0

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "total_chunks": self.total_chunks,
            "completed": self.completed,
            "failed": self.failed,
            "in_flight": sorted(self.in_flight),
            "errors": list(self.errors),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error_message": self.error_message,
            "format": self.format,
            "language": self.language,
            "source_filename": self.source_filename,
            "version": self.version,
        }

    def update(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def mark_terminal(self, status: str) -> None:
        with self._cond:
            self.status = status
            self.finished_at = time.time()
            self.in_flight.clear()
            self.version += 1
            self._cond.notify_all()

    def wait_for_change(self, last_version: int, timeout: float) -> bool:
        """Block the calling thread until version advances past
        last_version, or timeout elapses. Returns True if changed."""
        with self._cond:
            if self.version <= last_version:
                self._cond.wait(timeout=timeout)
            return self.version > last_version


# --- Registry ----------------------------------------------------------------
#
# The registry is a plain module-level dict. A single threading.Lock guards
# structural mutations (create / remove / acquire slot). Per-job state
# updates go through Job.update() which uses the per-job Condition.

_registry: dict[str, Job] = {}
_slot_lock = threading.Lock()


def get(job_id: str) -> Optional[Job]:
    return _registry.get(job_id)


def all_job_ids() -> list[str]:
    return list(_registry.keys())


def create_job(format: str, language: str, source_filename: str) -> Job:
    """Create and register a new job. Caller is responsible for actually
    running the transcription work afterwards via run_job()."""
    job = Job(
        job_id=uuid.uuid4().hex,
        format=format,
        language=language,
        source_filename=source_filename,
    )
    _registry[job.job_id] = job
    return job


def remove_job(job_id: str) -> None:
    with _slot_lock:
        _registry.pop(job_id, None)


def acquire_job_slot() -> bool:
    """Returns True if no job is currently running; False otherwise.

    Used to enforce the 'one job at a time' invariant. The caller is
    expected to create a job only after successfully acquiring the slot;
    the slot is implicitly released when the job reaches a terminal
    status and is removed (either by the reaper or by /result).
    """
    with _slot_lock:
        for job in _registry.values():
            if job.status == "running":
                return False
        return True


def reap_finished(now: Optional[float] = None) -> int:
    """Remove terminal jobs older than JOB_TTL_SECONDS. Returns the count
    reaped."""
    now = now if now is not None else time.time()
    stale = [
        jid
        for jid, job in _registry.items()
        if job.status in ("done", "error")
        and job.finished_at is not None
        and now - job.finished_at > JOB_TTL_SECONDS
    ]
    for jid in stale:
        with _slot_lock:
            _registry.pop(jid, None)
    return len(stale)


def reaper_loop(interval_seconds: float = 60.0) -> None:
    """Background thread that reaps stale terminal jobs periodically.
    Runs forever; daemonize the thread so it dies with the process."""
    while True:
        try:
            reap_finished()
        except Exception:
            pass
        time.sleep(interval_seconds)


def reset_for_tests() -> None:
    """Clear the registry between unit tests."""
    with _slot_lock:
        _registry.clear()
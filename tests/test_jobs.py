import time
from unittest import mock

import app as app_module
from app import jobs


def _make_job(job_id="j1", status="running", **kw):
    j = jobs.Job(job_id=job_id, format="text", language="en",
                source_filename="x.mp3", status=status)
    for k, v in kw.items():
        setattr(j, k, v)
    jobs._registry[job_id] = j
    return j


class TestJobSnapshot:
    def test_snapshot_round_trips_fields(self):
        j = _make_job(total_chunks=5, completed=2)
        snap = j.snapshot()
        assert snap["job_id"] == "j1"
        assert snap["status"] == "running"
        assert snap["total_chunks"] == 5
        assert snap["completed"] == 2
        assert snap["version"] == 0
        assert "in_flight" in snap
        assert "errors" in snap

    def test_update_bumps_version(self):
        j = _make_job()
        v0 = j.version
        j.update()
        assert j.version == v0 + 1

    def test_mark_terminal_sets_finished_at(self):
        j = _make_job()
        before = time.time()
        j.mark_terminal("done")
        after = time.time()
        assert j.status == "done"
        assert j.finished_at is not None
        assert before <= j.finished_at <= after
        assert j.in_flight == set()

    def test_wait_for_change_returns_true_when_already_changed(self):
        j = _make_job()
        v0 = j.version
        j.update()
        assert j.wait_for_change(v0, timeout=0.01) is True

    def test_wait_for_change_times_out_when_no_change(self):
        j = _make_job()
        v0 = j.version
        result = j.wait_for_change(v0, timeout=0.05)
        assert result is False


class TestRegistry:
    def test_get_returns_none_for_missing(self):
        assert jobs.get("nope") is None

    def test_get_returns_registered_job(self):
        j = _make_job("abc")
        assert jobs.get("abc") is j

    def test_remove_job_drops_entry(self):
        j = _make_job("xyz")
        jobs.remove_job("xyz")
        assert jobs.get("xyz") is None

    def test_reset_for_tests_clears_everything(self):
        _make_job("a")
        _make_job("b")
        assert len(jobs.all_job_ids()) == 2
        jobs.reset_for_tests()
        assert jobs.all_job_ids() == []


class TestAcquireSlot:
    def test_empty_registry_succeeds(self):
        jobs.reset_for_tests()
        assert jobs.acquire_job_slot() is True

    def test_running_job_blocks_slot(self):
        _make_job("running1", status="running")
        assert jobs.acquire_job_slot() is False

    def test_terminal_job_does_not_block_slot(self):
        _make_job("done1", status="done", finished_at=time.time())
        assert jobs.acquire_job_slot() is True


class TestReap:
    def test_old_terminal_job_is_reaped(self):
        old = time.time() - (jobs.JOB_TTL_SECONDS + 1)
        _make_job("old", status="done", finished_at=old)
        reaped = jobs.reap_finished()
        assert reaped == 1
        assert jobs.get("old") is None

    def test_recent_terminal_job_is_kept(self):
        _make_job("fresh", status="done", finished_at=time.time())
        reaped = jobs.reap_finished()
        assert reaped == 0
        assert jobs.get("fresh") is not None

    def test_running_job_is_not_reaped(self):
        _make_job("run", status="running")
        assert jobs.reap_finished() == 0
        assert jobs.get("run") is not None


class TestCreateJob:
    def test_create_job_returns_with_defaults(self):
        j = jobs.create_job(format="srt", language="ru", source_filename="x.mp4")
        assert j.job_id
        assert j.status == "running"
        assert j.total_chunks == 0
        assert j.completed == 0
        assert j.format == "srt"
        assert j.language == "ru"
        assert j.source_filename == "x.mp4"
        assert jobs.get(j.job_id) is j

    def test_create_job_id_is_unique(self):
        a = jobs.create_job(format="text", language="en", source_filename="a")
        b = jobs.create_job(format="text", language="en", source_filename="b")
        assert a.job_id != b.job_id
import time

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def _reset_job_registry():
    """Each test starts with an empty job registry so the single-running-job
    invariant doesn't leak between tests."""
    app_module.jobs.reset_for_tests()
    app_module._job_work_dirs.clear()
    yield
    app_module.jobs.reset_for_tests()
    app_module._job_work_dirs.clear()


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def wait_for_job():
    """Block (synchronously, on the calling thread) until the job reaches
    a terminal status.

    run_job runs on a dedicated worker thread/event-loop. The test thread
    just calls job.wait_for_change() (a threading.Condition wait) until
    status is no longer "running". An overall timeout guards against
    stuck jobs.
    """

    def _wait(job_id, timeout=5.0):
        job = app_module.jobs.get(job_id)
        if job is None:
            return None
        deadline = time.monotonic() + timeout
        while job.status == "running":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return job
            job.wait_for_change(last_version=job.version, timeout=min(0.1, remaining))
        return job

    return _wait
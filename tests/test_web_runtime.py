from __future__ import annotations

import threading

import pytest

from src.web import runtime


def test_stop_background_workers_clears_reference_only_after_thread_exits() -> None:
    started = threading.Event()

    def cooperative_worker() -> None:
        started.set()
        runtime._job_worker_stop.wait()

    runtime._job_worker_stop.clear()
    worker = threading.Thread(target=cooperative_worker, name="test-cooperative-worker")
    runtime._job_worker_thread = worker
    worker.start()

    try:
        assert started.wait(timeout=5)
        runtime.stop_background_workers(timeout=5)

        assert worker.is_alive() is False
        assert runtime._job_worker_thread is None
        assert runtime._job_worker_stop.is_set()
    finally:
        runtime._job_worker_stop.set()
        worker.join(timeout=5)
        runtime._job_worker_thread = None


def test_stop_background_workers_timeout_preserves_live_thread_and_prevents_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release_worker = threading.Event()
    resumed = threading.Event()

    def long_running_worker() -> None:
        started.set()
        release_worker.wait()
        resumed.set()
        runtime._job_worker_stop.wait()

    runtime._job_worker_stop.clear()
    worker = threading.Thread(target=long_running_worker, name="test-long-running-worker")
    runtime._job_worker_thread = worker
    worker.start()

    try:
        assert started.wait(timeout=5)
        with pytest.raises(RuntimeError, match="worker.*未在限定时间内停止"):
            runtime.stop_background_workers(timeout=0)

        assert worker.is_alive() is True
        assert runtime._job_worker_thread is worker
        assert runtime._job_worker_stop.is_set() is False

        monkeypatch.setattr(
            runtime.threading,
            "Thread",
            lambda *args, **kwargs: pytest.fail("must not start a second worker"),
        )
        monkeypatch.setattr(runtime, "_maybe_prewarm_first_run_auth", lambda: None)
        runtime.start_background_workers()

        assert runtime._job_worker_thread is worker
        release_worker.set()
        assert resumed.wait(timeout=5)
        assert worker.is_alive() is True
    finally:
        release_worker.set()
        runtime._job_worker_stop.set()
        worker.join(timeout=5)
        runtime._job_worker_thread = worker
        runtime.stop_background_workers(timeout=0)

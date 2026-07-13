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
    runtime._job_worker_shutdown_requested = False
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
        runtime._job_worker_shutdown_requested = False
        runtime._job_worker_stop.clear()


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
    runtime._job_worker_shutdown_requested = False
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
        runtime._job_worker_shutdown_requested = False
        runtime._job_worker_stop.clear()


def test_shutdown_workers_waits_for_exit_and_suppresses_concurrent_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exited = threading.Event()
    shutdown_returned = threading.Event()
    start_attempted = threading.Event()
    start_returned = threading.Event()

    def long_running_worker() -> None:
        worker_started.set()
        release_worker.wait()
        assert runtime._job_worker_stop.is_set()
        worker_exited.set()

    def request_shutdown() -> None:
        runtime.shutdown_workers()
        assert worker_exited.is_set()
        shutdown_returned.set()

    def attempt_start_during_shutdown() -> None:
        start_attempted.set()
        runtime.start_background_workers()
        start_returned.set()

    runtime._job_worker_stop.clear()
    runtime._job_worker_shutdown_requested = False
    worker = threading.Thread(target=long_running_worker, name="test-final-shutdown-worker")
    shutdown_thread = threading.Thread(target=request_shutdown, name="test-worker-shutdown")
    start_thread = threading.Thread(target=attempt_start_during_shutdown, name="test-worker-restart")
    runtime._job_worker_thread = worker
    worker.start()

    monkeypatch.setattr(
        runtime.jobs,
        "enqueue_pending_search_reindexes",
        lambda: pytest.fail("shutdown must suppress a concurrent worker start"),
    )
    monkeypatch.setattr(
        runtime,
        "_maybe_prewarm_first_run_auth",
        lambda: pytest.fail("shutdown must suppress startup prewarm"),
    )

    try:
        assert worker_started.wait(timeout=5)
        shutdown_thread.start()
        assert runtime._job_worker_stop.wait(timeout=5)
        assert runtime._job_worker_stop.is_set()
        assert shutdown_returned.is_set() is False

        start_thread.start()
        assert start_attempted.wait(timeout=5)
        assert start_returned.is_set() is False

        release_worker.set()
        assert worker_exited.wait(timeout=5)
        assert shutdown_returned.wait(timeout=5)
        assert start_returned.wait(timeout=5)

        assert runtime._job_worker_thread is None
        assert runtime._job_worker_stop.is_set()
        assert runtime._job_worker_shutdown_requested is True
    finally:
        release_worker.set()
        runtime._job_worker_stop.set()
        worker.join(timeout=5)
        shutdown_thread.join(timeout=5)
        start_thread.join(timeout=5)
        runtime._job_worker_thread = None
        runtime._job_worker_shutdown_requested = False
        runtime._job_worker_stop.clear()


def test_shutdown_workers_waits_for_inflight_start_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    worker_exited = threading.Event()
    prewarm_started = threading.Event()
    release_prewarm = threading.Event()
    prewarm_finished = threading.Event()
    start_returned = threading.Event()
    shutdown_attempted = threading.Event()
    shutdown_blocked_by_start = threading.Event()
    shutdown_acquired_before_prewarm_finished = threading.Event()
    shutdown_returned = threading.Event()

    class ObservedLifecycleLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "test-prewarm-shutdown":
                shutdown_attempted.set()
                if self._lock.acquire(blocking=False):
                    shutdown_acquired_before_prewarm_finished.set()
                else:
                    shutdown_blocked_by_start.set()
                    self._lock.acquire()
            else:
                self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            self._lock.release()

    def cooperative_worker() -> None:
        worker_started.set()
        runtime._job_worker_stop.wait()
        worker_exited.set()

    def blocking_prewarm() -> None:
        prewarm_started.set()
        if not release_prewarm.wait(timeout=5):
            raise AssertionError("test did not release startup prewarm")
        prewarm_finished.set()

    def request_start() -> None:
        runtime.start_background_workers()
        start_returned.set()

    def request_shutdown() -> None:
        runtime.shutdown_workers()
        assert prewarm_finished.is_set()
        shutdown_returned.set()

    runtime._job_worker_stop.clear()
    runtime._job_worker_shutdown_requested = False
    worker = threading.Thread(target=cooperative_worker, name="test-prewarm-worker")
    start_thread = threading.Thread(target=request_start, name="test-prewarm-start")
    shutdown_thread = threading.Thread(target=request_shutdown, name="test-prewarm-shutdown")
    runtime._job_worker_thread = worker

    monkeypatch.setattr(runtime, "_job_worker_lifecycle_lock", ObservedLifecycleLock())
    monkeypatch.setattr(runtime, "_maybe_prewarm_first_run_auth", blocking_prewarm)

    worker.start()
    try:
        assert worker_started.wait(timeout=5)
        start_thread.start()
        assert prewarm_started.wait(timeout=5)

        shutdown_thread.start()
        assert shutdown_attempted.wait(timeout=5)
        assert shutdown_blocked_by_start.is_set()
        assert shutdown_acquired_before_prewarm_finished.is_set() is False
        assert shutdown_returned.is_set() is False
        assert runtime._job_worker_stop.is_set() is False

        release_prewarm.set()
        assert prewarm_finished.wait(timeout=5)
        assert start_returned.wait(timeout=5)
        assert worker_exited.wait(timeout=5)
        assert shutdown_returned.wait(timeout=5)

        assert runtime._job_worker_thread is None
        assert runtime._job_worker_stop.is_set()
        assert runtime._job_worker_shutdown_requested is True
    finally:
        release_prewarm.set()
        runtime._job_worker_stop.set()
        worker.join(timeout=5)
        start_thread.join(timeout=5)
        shutdown_thread.join(timeout=5)
        runtime._job_worker_thread = None
        runtime._job_worker_shutdown_requested = False
        runtime._job_worker_stop.clear()

"""Web background-worker lifecycle."""
from __future__ import annotations

import threading

from loguru import logger

from src import accounts, jobs, onboarding
from src.config import settings
from src.tenancy import normalize_user_id
from src.web import douyin_auth


_job_worker_stop = threading.Event()
_job_worker_thread: threading.Thread | None = None
_job_worker_lifecycle_lock = threading.Lock()


def _maybe_prewarm_first_run_auth() -> None:
    if settings.web_auth_required:
        return
    try:
        user = accounts.ensure_default_user()
        user_id = normalize_user_id(user["id"])
        status = onboarding.get_onboarding_status(user_id)
        if douyin_auth.should_auto_start_setup_auth(status):
            douyin_auth.ensure_douyin_auth_started(user_id)
    except Exception as exc:
        logger.warning("Could not prewarm first-run Douyin auth: {}", exc)


def start_background_workers() -> None:
    global _job_worker_thread
    with _job_worker_lifecycle_lock:
        if _job_worker_thread is None or not _job_worker_thread.is_alive():
            reindex_job_ids = jobs.enqueue_pending_search_reindexes()
            if reindex_job_ids:
                logger.info(
                    "Queued {} durable search index recovery job(s) before worker startup.",
                    len(reindex_job_ids),
                )
            recovered = jobs.recover_stale_running_jobs(stale_after_seconds=0)
            if recovered:
                logger.info("Recovered {} interrupted background job(s) before worker startup.", recovered)
            _job_worker_stop.clear()
            _job_worker_thread = threading.Thread(
                target=jobs.run_forever,
                kwargs={"stop_event": _job_worker_stop, "poll_interval": 1.0},
                name="recall-job-worker",
                daemon=True,
            )
            _job_worker_thread.start()
    _maybe_prewarm_first_run_auth()


def stop_background_workers(*, timeout: float = 5.0) -> None:
    """Stop the job worker or fail without forgetting a still-running thread."""
    global _job_worker_thread
    with _job_worker_lifecycle_lock:
        _job_worker_stop.set()
        worker = _job_worker_thread
        if worker is None:
            return
        worker.join(timeout=max(0.0, float(timeout)))
        if worker.is_alive():
            # No database cutover has happened yet. Cancel the stop request so
            # the existing worker resumes after its current long-running job.
            _job_worker_stop.clear()
            raise RuntimeError(
                "后台任务 worker 未在限定时间内停止；已保留线程引用，"
                "不能安全执行数据库维护。"
            )
        _job_worker_thread = None


def shutdown_workers() -> None:
    stop_background_workers()

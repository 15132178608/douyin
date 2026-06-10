"""Small SQLite-backed background job queue."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import traceback
from typing import Any

from loguru import logger

from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


DEFAULT_STALE_RUNNING_SECONDS = 60 * 60
MAX_RETRY_DELAY_SECONDS = 60 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay_seconds(attempts: int) -> int:
    attempt_index = max(0, int(attempts or 0) - 1)
    return min(MAX_RETRY_DELAY_SECONDS, 60 * (2 ** attempt_index))


def enqueue_job(
    kind: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO job_queue (
            user_id, kind, payload_json, status, max_attempts, created_at
        ) VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (
            normalize_user_id(user_id),
            kind,
            json.dumps(payload or {}, ensure_ascii=False),
            max(1, int(max_attempts or 1)),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def claim_next_job() -> dict | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
        FROM job_queue
        WHERE status = 'pending'
          AND (next_run_at IS NULL OR next_run_at <= ?)
        ORDER BY COALESCE(next_run_at, created_at) ASC, created_at ASC, id ASC
        LIMIT 1
        """,
        (_now(),),
    ).fetchone()
    if row is None:
        return None
    now = _now()
    updated = conn.execute(
        """
        UPDATE job_queue
        SET status = 'running',
            attempts = attempts + 1,
            started_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now, row["id"]),
    )
    if updated.rowcount == 0:
        return None
    claimed = conn.execute("SELECT * FROM job_queue WHERE id = ?", (row["id"],)).fetchone()
    payload_text = claimed["payload_json"] or "{}"
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        payload = {}
    data = dict(claimed)
    data["payload"] = payload
    return data


def finish_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE job_queue
        SET status = 'success',
            finished_at = ?,
            error_message = NULL
        WHERE id = ?
        """,
        (_now(), job_id),
    )


def fail_job(job_id: int, error_message: str) -> None:
    conn = get_connection()
    row = conn.execute(
        "SELECT attempts, max_attempts FROM job_queue WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return
    now = _now()
    attempts = int(row["attempts"] or 0)
    max_attempts = max(1, int(row["max_attempts"] or 1))
    if attempts < max_attempts:
        delay_seconds = _retry_delay_seconds(attempts)
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'pending',
                next_run_at = ?,
                started_at = NULL,
                finished_at = NULL,
                error_message = ?
            WHERE id = ?
            """,
            (datetime.fromtimestamp(now.timestamp() + delay_seconds, timezone.utc), str(error_message), job_id),
        )
        return
    conn.execute(
        """
        UPDATE job_queue
        SET status = 'failed',
            finished_at = ?,
            error_message = ?
        WHERE id = ?
        """,
        (now, str(error_message), job_id),
    )


def recover_stale_running_jobs(*, stale_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS) -> int:
    """Put old running jobs back in the queue so a crashed worker does not stall them forever."""
    conn = get_connection()
    now = _now()
    threshold = datetime.fromtimestamp(now.timestamp() - max(1, int(stale_after_seconds)), timezone.utc)
    stale_rows = conn.execute(
        """
        SELECT id, attempts, max_attempts
        FROM job_queue
        WHERE status = 'running'
          AND started_at IS NOT NULL
          AND started_at <= ?
        """,
        (threshold,),
    ).fetchall()
    recovered = 0
    for row in stale_rows:
        attempts = int(row["attempts"] or 0)
        max_attempts = max(1, int(row["max_attempts"] or 1))
        if attempts >= max_attempts:
            conn.execute(
                """
                UPDATE job_queue
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (now, "stale running job reached max attempts", row["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE job_queue
                SET status = 'pending',
                    next_run_at = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    error_message = ?
                WHERE id = ?
                """,
                (now, "stale running job recovered", row["id"]),
            )
        recovered += 1
    return recovered


def list_jobs(user_id: str = DEFAULT_USER_ID, *, limit: int = 50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, user_id, kind, payload_json, status, attempts, max_attempts,
               created_at, next_run_at, started_at, finished_at, error_message
        FROM job_queue
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (normalize_user_id(user_id), max(1, int(limit or 50))),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        out.append(item)
    return out


class DefaultJobHandlers:
    """Real background job implementations."""

    def _refresh_douyin_profile(self, user_id: str, crawler: Any) -> None:
        from src import accounts

        try:
            profile = crawler.get_self_profile()
        except Exception as e:
            logger.warning("Could not refresh Douyin profile for {}: {}", user_id, e)
            return
        if profile:
            accounts.update_douyin_profile(user_id, profile)

    def sync_favorites(self, user_id: str, payload: dict) -> None:
        from src import accounts
        from src.crawler.douyin import DouyinCrawler
        from src.crawler.sync import apply_crawl, record_crawl_run

        started = _now()
        max_pages = int(payload.get("max_pages") or 500)
        allow_large_removal = bool(payload.get("allow_large_removal") or False)
        try:
            with DouyinCrawler(
                headless=True,
                api_mode=True,
                max_api_pages=max_pages,
                hide_window=True,
                browser_channel="chrome",
                profile_path=accounts.profile_path_for_user(user_id),
            ) as crawler:
                self._refresh_douyin_profile(user_id, crawler)
                favorites = crawler.crawl_collection()
            result = apply_crawl(
                favorites,
                user_id=user_id,
                allow_large_removal=allow_large_removal,
            )
            record_crawl_run(started, _now(), "success", result, user_id=user_id)
        except Exception as e:
            record_crawl_run(started, _now(), "failed", error_message=str(e), user_id=user_id)
            raise

    def sync_likes(self, user_id: str, payload: dict) -> None:
        from src import accounts
        from src.crawler.douyin import DouyinCrawler
        from src.crawler.sync import apply_like_crawl, record_crawl_run_for_kind

        started = _now()
        max_pages = int(payload.get("max_pages") or 500)
        allow_large_removal = bool(payload.get("allow_large_removal") or False)
        try:
            with DouyinCrawler(
                headless=True,
                api_mode=True,
                max_api_pages=max_pages,
                hide_window=True,
                browser_channel="chrome",
                profile_path=accounts.profile_path_for_user(user_id),
            ) as crawler:
                self._refresh_douyin_profile(user_id, crawler)
                likes = crawler.crawl_likes()
            result = apply_like_crawl(
                likes,
                user_id=user_id,
                allow_large_removal=allow_large_removal,
            )
            record_crawl_run_for_kind("likes", started, _now(), "success", result, user_id=user_id)
        except Exception as e:
            record_crawl_run_for_kind("likes", started, _now(), "failed", error_message=str(e), user_id=user_id)
            raise

    def index(self, user_id: str, payload: dict) -> None:
        from src.embedding.indexer import index_all

        index_all(
            batch_size=int(payload.get("batch_size") or 32),
            force=bool(payload.get("force") or False),
            content_kind=payload.get("content_kind") or "favorites",
            user_id=user_id,
        )

    def uncollect(self, user_id: str, payload: dict) -> None:
        from src import accounts
        from src.uncollector.douyin import PersistentUncollectWorker

        aweme_id = str(payload.get("aweme_id") or "").strip()
        if not aweme_id:
            raise ValueError("uncollect job missing aweme_id")
        content_kind = payload.get("content_kind") or "favorites"
        log_id = payload.get("log_id")
        worker = PersistentUncollectWorker(
            profile_path=accounts.profile_path_for_user(user_id),
            browser_channel="chrome",
        )
        try:
            if content_kind == "likes":
                result = worker.unlike_one(aweme_id, dry_run=False)
                _mark_uncollect_result(user_id, "likes", aweme_id, result.success, result.message, log_id=log_id)
            else:
                result = worker.uncollect_one(aweme_id, dry_run=False)
                _mark_uncollect_result(user_id, "favorites", aweme_id, result.success, result.message, log_id=log_id)
            if not result.success:
                raise RuntimeError(result.message)
        finally:
            worker.close()


def _mark_uncollect_result(
    user_id: str,
    content_kind: str,
    aweme_id: str,
    success: bool,
    message: str,
    log_id: int | None = None,
) -> None:
    conn = get_connection()
    now = _now()
    if content_kind == "likes":
        if log_id:
            conn.execute(
                """
                UPDATE unlike_log
                SET finished_at = ?, status = ?, error_message = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, "success" if success else "failed", None if success else message, int(log_id), user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO unlike_log (user_id, like_id, initiated_at, finished_at, status, channel, error_message)
                VALUES (?, ?, ?, ?, ?, 'job', ?)
                """,
                (user_id, aweme_id, now, now, "success" if success else "failed", None if success else message),
            )
        if success:
            conn.execute(
                "UPDATE likes SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
                (now, user_id, aweme_id),
            )
    else:
        if log_id:
            conn.execute(
                """
                UPDATE uncollect_log
                SET finished_at = ?, status = ?, error_message = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, "success" if success else "failed", None if success else message, int(log_id), user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO uncollect_log (user_id, favorite_id, initiated_at, finished_at, status, channel, error_message)
                VALUES (?, ?, ?, ?, ?, 'job', ?)
                """,
                (user_id, aweme_id, now, now, "success" if success else "failed", None if success else message),
            )
        if success:
            conn.execute(
                "UPDATE favorites SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
                (now, user_id, aweme_id),
            )


def run_next_job(handlers: Any | None = None) -> bool:
    recover_stale_running_jobs()
    job = claim_next_job()
    if job is None:
        return False

    active_handlers = handlers or DefaultJobHandlers()
    try:
        handler = getattr(active_handlers, job["kind"])
    except AttributeError as e:
        fail_job(job["id"], f"Unknown job kind: {job['kind']}")
        raise RuntimeError(f"Unknown job kind: {job['kind']}") from e

    try:
        handler(job["user_id"], job["payload"])
    except Exception as e:
        fail_job(job["id"], f"{e}\n{traceback.format_exc()}")
    else:
        finish_job(job["id"])
    return True


def run_forever(
    handlers: Any | None = None,
    *,
    poll_interval: float = 2.0,
    stop_event: Any | None = None,
) -> None:
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        did_work = run_next_job(handlers)
        if not did_work:
            time.sleep(poll_interval)

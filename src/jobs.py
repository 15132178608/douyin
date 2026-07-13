"""Small SQLite-backed background job queue."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import traceback
from typing import Any

from loguru import logger

from src.config import PROJECT_ROOT
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


DEFAULT_STALE_RUNNING_SECONDS = 60 * 60
MAX_RETRY_DELAY_SECONDS = 60 * 60
DURABLE_SEARCH_REINDEX_DELAY_SECONDS = 15 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay_seconds(attempts: int) -> int:
    attempt_index = max(0, int(attempts or 0) - 1)
    return min(MAX_RETRY_DELAY_SECONDS, 60 * (2 ** attempt_index))


def _canonical_payload_json(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def _decode_payload_object(raw: Any) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_durable_search_reindex_job(conn: Any, row: Any) -> bool:
    if row["kind"] != "index":
        return False
    payload = _decode_payload_object(row["payload_json"])
    if payload.get("force") is not True:
        return False
    if payload.get("schema_reindex") is True:
        # Schema-recovery jobs remain replayable until the queue row itself is
        # committed as success, including the small crash window after the
        # durable marker was completed.
        return True
    content_kind = str(payload.get("content_kind") or "favorites")
    try:
        marker = conn.execute(
            """
            SELECT 1
            FROM search_reindex_state
            WHERE user_id = ?
              AND content_kind = ?
              AND completed_at IS NULL
            """,
            (normalize_user_id(row["user_id"]), content_kind),
        ).fetchone()
    except sqlite3.Error as exc:
        # Conservatively retain a force-index job when marker verification is
        # temporarily unavailable. Terminal failure here could leave an
        # upgraded user's search partition permanently empty.
        logger.warning("Could not verify durable search reindex marker: {}", exc)
        return True
    return marker is not None


def _find_open_duplicate_job_id(
    *,
    conn: Any,
    user_id: str,
    kind: str,
    payload_json: str,
) -> int | None:
    rows = conn.execute(
        """
        SELECT id, payload_json
        FROM job_queue
        WHERE user_id = ?
          AND kind = ?
          AND status IN ('pending', 'running')
        ORDER BY id ASC
        """,
        (user_id, kind),
    ).fetchall()
    for row in rows:
        try:
            existing_payload_json = _canonical_payload_json(json.loads(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            existing_payload_json = row["payload_json"] or "{}"
        if existing_payload_json == payload_json:
            return int(row["id"])
    return None


def enqueue_job(
    kind: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
    suppress_duplicate: bool = True,
    connection: sqlite3.Connection | None = None,
) -> int:
    conn = connection if connection is not None else get_connection()
    uid = normalize_user_id(user_id)
    payload_json = _canonical_payload_json(payload)
    owns_transaction = False
    try:
        if suppress_duplicate and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            owns_transaction = True
        if suppress_duplicate:
            existing_id = _find_open_duplicate_job_id(
                conn=conn,
                user_id=uid,
                kind=kind,
                payload_json=payload_json,
            )
            if existing_id is not None:
                if owns_transaction:
                    conn.execute("COMMIT")
                return existing_id
        cur = conn.execute(
            """
            INSERT INTO job_queue (
                user_id, kind, payload_json, status, max_attempts, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (
                uid,
                kind,
                payload_json,
                max(1, int(max_attempts or 1)),
                _now(),
            ),
        )
        if owns_transaction:
            conn.execute("COMMIT")
        return int(cur.lastrowid)
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def enqueue_pending_search_reindexes() -> list[int]:
    """Materialize durable schema-recovery markers as idempotent force-index jobs."""
    from src import db

    job_ids: list[int] = []
    for item in db.list_pending_search_reindexes():
        job_ids.append(
            enqueue_job(
                "index",
                user_id=item["user_id"],
                payload={
                    "content_kind": item["content_kind"],
                    "force": True,
                    "schema_reindex": True,
                },
            )
        )
    return job_ids


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
    payload = _decode_payload_object(claimed["payload_json"])
    data = dict(claimed)
    data["payload"] = payload
    return data


def _lease_condition(
    attempts: int | None,
    started_at: Any | None,
) -> tuple[str, tuple[Any, ...]]:
    if attempts is None and started_at is None:
        return "", ()
    if attempts is None or started_at is None:
        raise ValueError("attempts and started_at must be provided together")
    return (
        " AND status = 'running' AND attempts = ? AND started_at = ?",
        (int(attempts), started_at),
    )


def finish_job(
    job_id: int,
    *,
    attempts: int | None = None,
    started_at: Any | None = None,
) -> bool:
    conn = get_connection()
    lease_sql, lease_params = _lease_condition(attempts, started_at)
    updated = conn.execute(
        f"""
        UPDATE job_queue
        SET status = 'success',
            finished_at = ?,
            error_message = NULL
        WHERE id = ?{lease_sql}
        """,
        (_now(), job_id, *lease_params),
    )
    return updated.rowcount == 1


def fail_job(
    job_id: int,
    error_message: str,
    *,
    attempts: int | None = None,
    started_at: Any | None = None,
) -> bool:
    conn = get_connection()
    lease_sql, lease_params = _lease_condition(attempts, started_at)
    row = conn.execute(
        f"""
        SELECT user_id, kind, payload_json, attempts, max_attempts
        FROM job_queue
        WHERE id = ?{lease_sql}
        """,
        (job_id, *lease_params),
    ).fetchone()
    if row is None:
        return False
    now = _now()
    attempts = int(row["attempts"] or 0)
    max_attempts = max(1, int(row["max_attempts"] or 1))
    durable_reindex = _is_durable_search_reindex_job(conn, row)
    if attempts < max_attempts or durable_reindex:
        exhausted = attempts >= max_attempts
        delay_seconds = (
            DURABLE_SEARCH_REINDEX_DELAY_SECONDS
            if exhausted
            else _retry_delay_seconds(attempts)
        )
        next_max_attempts = attempts + 1 if exhausted else max_attempts
        updated = conn.execute(
            f"""
            UPDATE job_queue
            SET status = 'pending',
                max_attempts = ?,
                next_run_at = ?,
                started_at = NULL,
                finished_at = NULL,
                error_message = ?
            WHERE id = ?{lease_sql}
            """,
            (
                next_max_attempts,
                datetime.fromtimestamp(now.timestamp() + delay_seconds, timezone.utc),
                str(error_message),
                job_id,
                *lease_params,
            ),
        )
        return updated.rowcount == 1
    updated = conn.execute(
        f"""
        UPDATE job_queue
        SET status = 'failed',
            finished_at = ?,
            error_message = ?
        WHERE id = ?{lease_sql}
        """,
        (now, str(error_message), job_id, *lease_params),
    )
    return updated.rowcount == 1


def recover_stale_running_jobs(
    *,
    stale_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
    user_id: str | None = None,
) -> int:
    """Put old running jobs back in the queue so a crashed worker does not stall them forever."""
    conn = get_connection()
    now = _now()
    threshold = datetime.fromtimestamp(now.timestamp() - max(0, int(stale_after_seconds)), timezone.utc)
    params: list[Any] = [threshold]
    user_clause = ""
    if user_id is not None:
        user_clause = "AND user_id = ?"
        params.append(normalize_user_id(user_id))
    stale_rows = conn.execute(
        f"""
        SELECT id, user_id, kind, payload_json, attempts, max_attempts, started_at
        FROM job_queue
        WHERE status = 'running'
          AND started_at IS NOT NULL
          AND started_at <= ?
          {user_clause}
        """,
        tuple(params),
    ).fetchall()
    recovered = 0
    for row in stale_rows:
        attempts = int(row["attempts"] or 0)
        max_attempts = max(1, int(row["max_attempts"] or 1))
        durable_reindex = _is_durable_search_reindex_job(conn, row)
        if attempts >= max_attempts and not durable_reindex:
            updated = conn.execute(
                """
                UPDATE job_queue
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                  AND status = 'running'
                  AND started_at = ?
                  AND attempts = ?
                """,
                (
                    now,
                    "stale running job reached max attempts",
                    row["id"],
                    row["started_at"],
                    attempts,
                ),
            )
        else:
            exhausted = attempts >= max_attempts
            delay_seconds = DURABLE_SEARCH_REINDEX_DELAY_SECONDS if exhausted else 0
            next_max_attempts = attempts + 1 if exhausted else max_attempts
            updated = conn.execute(
                """
                UPDATE job_queue
                SET status = 'pending',
                    max_attempts = ?,
                    next_run_at = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    error_message = ?
                WHERE id = ?
                  AND status = 'running'
                  AND started_at = ?
                  AND attempts = ?
                """,
                (
                    next_max_attempts,
                    datetime.fromtimestamp(now.timestamp() + delay_seconds, timezone.utc),
                    "stale running job recovered",
                    row["id"],
                    row["started_at"],
                    attempts,
                ),
            )
        if updated.rowcount == 1:
            recovered += 1
    return recovered


def list_jobs(
    user_id: str = DEFAULT_USER_ID,
    *,
    limit: int = 50,
    recover_stale: bool = True,
) -> list[dict]:
    uid = normalize_user_id(user_id)
    if recover_stale:
        recover_stale_running_jobs(user_id=uid)
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
        (uid, max(1, int(limit or 50))),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["payload"] = _decode_payload_object(item.get("payload_json"))
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
        from src import db
        from src.embedding.indexer import index_all

        content_kind = payload.get("content_kind") or "favorites"
        schema_reindex = payload.get("schema_reindex") is True
        if schema_reindex:
            still_pending = any(
                item["user_id"] == user_id and item["content_kind"] == content_kind
                for item in db.list_pending_search_reindexes()
            )
            if not still_pending:
                logger.info(
                    "Skipping replay of completed search schema reindex for {} {}",
                    user_id,
                    content_kind,
                )
                return
        index_all(
            batch_size=int(payload.get("batch_size") or 32),
            force=bool(payload.get("force") or False),
            content_kind=content_kind,
            user_id=user_id,
        )
        if bool(payload.get("force") or False):
            pending = any(
                item["user_id"] == user_id and item["content_kind"] == content_kind
                for item in db.list_pending_search_reindexes()
            )
            if pending and not db.complete_search_reindex(user_id, content_kind):
                counts = db.search_index_counts(user_id, content_kind)
                raise RuntimeError(
                    "search reindex verification failed: "
                    f"active={counts['active']} vector={counts['vector']} fts={counts['fts']}"
                )

    def categorize(self, user_id: str, payload: dict) -> None:
        from src.categorize import cluster as cluster_mod

        cluster_mod.categorize_all(
            algo=payload.get("algo") or "kmeans",
            account_id=user_id,
            content_kind=payload.get("content_kind") or "favorites",
        )

    def backup_sqlite(self, user_id: str, payload: dict) -> None:
        from src import maintenance

        output_dir = Path(payload.get("output_dir") or (PROJECT_ROOT / "data" / "exports"))
        result = maintenance.create_sqlite_backup(output_dir)
        logger.info("SQLite backup created for {}: {} ({} rows)", user_id, result.path, result.count)

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
        fail_job(
            job["id"],
            f"Unknown job kind: {job['kind']}",
            attempts=job["attempts"],
            started_at=job["started_at"],
        )
        raise RuntimeError(f"Unknown job kind: {job['kind']}") from e

    try:
        handler(job["user_id"], job["payload"])
    except Exception as e:
        fail_job(
            job["id"],
            f"{e}\n{traceback.format_exc()}",
            attempts=job["attempts"],
            started_at=job["started_at"],
        )
    else:
        finish_job(
            job["id"],
            attempts=job["attempts"],
            started_at=job["started_at"],
        )
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

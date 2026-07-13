"""Transactional item mutations shared by Web action routes."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
import sqlite3

from src import jobs
from src.content.kinds import get_content_kind
from src.tenancy import normalize_user_id
from src.web.helpers import get_connection


_SAVEPOINT_SEQUENCE = count()
_SUPPORTED_CONTENT_KINDS = frozenset({"favorites", "likes"})


@dataclass(frozen=True)
class RemovalBatchResult:
    queued_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    already_removed_ids: tuple[str, ...]
    job_ids: tuple[int, ...]

    @property
    def queued_count(self) -> int:
        return len(self.queued_ids)


def _resolve_content_kind(content_kind: str):
    if content_kind not in _SUPPORTED_CONTENT_KINDS:
        raise ValueError(f"unsupported content kind: {content_kind!r}")
    return get_content_kind(content_kind)


@contextmanager
def _transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = True,
) -> Iterator[None]:
    """Own a transaction, or isolate this unit inside the caller's transaction."""
    owns_transaction = not connection.in_transaction
    savepoint = ""
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    else:
        savepoint = f"web_item_action_{next(_SAVEPOINT_SEQUENCE)}"
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
        if owns_transaction:
            connection.execute("COMMIT")
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        if owns_transaction:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def queue_item_removals(
    content_kind: str,
    user_id: str,
    item_ids: Iterable[str],
    *,
    channel: str,
    limit: int = 50,
    connection: sqlite3.Connection | None = None,
) -> RemovalBatchResult:
    """Atomically mark items removed, append audit rows, and enqueue remote work."""
    kind = _resolve_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    unique_ids = tuple(
        item_id
        for item_id in dict.fromkeys(item_ids or ())
        if item_id
    )[: max(0, int(limit))]
    conn = connection if connection is not None else get_connection()
    queued_ids: list[str] = []
    missing_ids: list[str] = []
    already_removed_ids: list[str] = []
    job_ids: list[int] = []
    initiated_at = datetime.now(timezone.utc)

    with _transaction(conn, immediate=True):
        for item_id in unique_ids:
            updated = conn.execute(
                f"""
                UPDATE {kind.table}
                SET is_removed = 1, last_seen_at = ?
                WHERE user_id = ? AND id = ? AND is_removed = 0
                """,
                (initiated_at, uid, item_id),
            )
            if updated.rowcount not in (0, 1):
                raise RuntimeError(
                    f"item removal updated {updated.rowcount} rows for {kind.key}/{item_id}"
                )
            if updated.rowcount == 0:
                row = conn.execute(
                    f"SELECT is_removed FROM {kind.table} WHERE user_id = ? AND id = ?",
                    (uid, item_id),
                ).fetchone()
                if row is None:
                    missing_ids.append(item_id)
                else:
                    already_removed_ids.append(item_id)
                continue

            if kind.key == "likes":
                cursor = conn.execute(
                    "INSERT INTO unlike_log "
                    "(user_id, like_id, initiated_at, status, channel) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (uid, item_id, initiated_at, channel),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO uncollect_log "
                    "(user_id, favorite_id, initiated_at, status, channel) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (uid, item_id, initiated_at, channel),
                )
            job_ids.append(
                jobs.enqueue_job(
                    "uncollect",
                    user_id=uid,
                    payload={
                        "content_kind": kind.key,
                        "aweme_id": item_id,
                        "log_id": cursor.lastrowid,
                    },
                    suppress_duplicate=False,
                    connection=conn,
                )
            )
            queued_ids.append(item_id)

    return RemovalBatchResult(
        queued_ids=tuple(queued_ids),
        missing_ids=tuple(missing_ids),
        already_removed_ids=tuple(already_removed_ids),
        job_ids=tuple(job_ids),
    )


def track_item_open(
    content_kind: str,
    user_id: str,
    item_id: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Record an item open atomically; return False when the tenant cannot access it."""
    kind = _resolve_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    conn = connection if connection is not None else get_connection()
    recalled_at = datetime.now(timezone.utc)
    found = False

    with _transaction(conn, immediate=True):
        updated = conn.execute(
            f"UPDATE {kind.table} SET last_recalled_at = ? WHERE user_id = ? AND id = ?",
            (recalled_at, uid, item_id),
        )
        found = updated.rowcount == 1
        if found and kind.key == "favorites":
            conn.execute(
                "INSERT INTO recall_log (user_id, favorite_id, recalled_at, channel, user_action) "
                "VALUES (?, ?, ?, 'search', 'opened')",
                (uid, item_id, recalled_at),
            )
        elif found:
            conn.execute(
                "INSERT INTO like_recall_log (user_id, like_id, recalled_at, channel, user_action) "
                "VALUES (?, ?, ?, 'search', 'opened')",
                (uid, item_id, recalled_at),
            )

    return found

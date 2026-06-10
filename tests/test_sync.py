"""
Crawler sync tests.

Run:
    python tests/test_sync.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.crawler import sync
from src.db import SCHEMA_SQL
from src.models import Favorite


@contextmanager
def isolated_sync_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)

    original_get_connection = sync.get_connection
    original_transaction = sync.transaction

    def get_connection():
        return conn

    @contextmanager
    def transaction():
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    sync.get_connection = get_connection
    sync.transaction = transaction
    try:
        yield conn
    finally:
        sync.get_connection = original_get_connection
        sync.transaction = original_transaction
        conn.close()


def insert_favorite(conn, favorite_id: str, *, is_removed: int = 0, title: str = "old") -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO favorites (
            id, title, first_seen_at, last_seen_at, raw_json, is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (favorite_id, title, now, now, "{}", is_removed, 1),
    )


def insert_like(conn, like_id: str, *, is_removed: int = 0, title: str = "old") -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO likes (
            id, title, first_seen_at, last_seen_at, raw_json, is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (like_id, title, now, now, "{}", is_removed, 1),
    )


def active_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM favorites WHERE is_removed = 0").fetchone()["c"]


def removed_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM favorites WHERE is_removed = 1").fetchone()["c"]


def test_removed_favorite_reappearing_is_reactivated_not_inserted() -> None:
    with isolated_sync_db() as conn:
        insert_favorite(conn, "aweme-1", is_removed=1, title="old title")

        result = sync.apply_crawl([Favorite(id="aweme-1", title="new title")], is_first_crawl=False)

        row = conn.execute(
            "SELECT title, is_removed FROM favorites WHERE id = ?",
            ("aweme-1",),
        ).fetchone()
        assert result.new_count == 0
        assert result.updated_count == 1
        assert row["title"] == "new title"
        assert row["is_removed"] == 0


def test_suspicious_large_removed_set_aborts_without_marking_removed() -> None:
    with isolated_sync_db() as conn:
        for i in range(100):
            insert_favorite(conn, f"old-{i}", is_removed=0)

        try:
            sync.apply_crawl(
                [Favorite(id=f"old-{i}", title=f"kept {i}") for i in range(10)],
                is_first_crawl=False,
            )
        except RuntimeError as e:
            assert "Suspicious crawl removal" in str(e)
        else:
            raise AssertionError("expected suspicious removal guard to abort")

        assert active_count(conn) == 100
        assert removed_count(conn) == 0


def test_large_removed_set_can_be_confirmed_explicitly() -> None:
    with isolated_sync_db() as conn:
        for i in range(100):
            insert_favorite(conn, f"old-{i}", is_removed=0)

        result = sync.apply_crawl(
            [Favorite(id=f"old-{i}", title=f"kept {i}") for i in range(10)],
            is_first_crawl=False,
            allow_large_removal=True,
        )

        assert result.updated_count == 10
        assert result.removed_count == 90
        assert active_count(conn) == 10
        assert removed_count(conn) == 90


def test_apply_like_crawl_writes_likes_without_touching_favorites() -> None:
    with isolated_sync_db() as conn:
        result = sync.apply_like_crawl(
            [Favorite(id="liked-1", title="liked title")],
            is_first_crawl=True,
        )

        row = conn.execute(
            "SELECT title, liked_at, is_removed, discovery_index FROM likes WHERE id = ?",
            ("liked-1",),
        ).fetchone()
        favorite_count = conn.execute("SELECT COUNT(*) AS c FROM favorites").fetchone()["c"]

        assert result.new_count == 1
        assert result.updated_count == 0
        assert result.removed_count == 0
        assert row["title"] == "liked title"
        assert row["liked_at"] is None
        assert row["is_removed"] == 0
        assert row["discovery_index"] == 0
        assert favorite_count == 0


def test_like_crawl_run_is_recorded_separately() -> None:
    with isolated_sync_db() as conn:
        started = datetime.now(timezone.utc)
        finished = datetime.now(timezone.utc)
        result = sync.apply_like_crawl([Favorite(id="liked-1")], is_first_crawl=True)

        sync.record_crawl_run_for_kind("likes", started, finished, "success", result)

        likes_run = conn.execute(
            "SELECT new_count, updated_count, removed_count FROM like_crawl_runs"
        ).fetchone()
        favorite_run_count = conn.execute("SELECT COUNT(*) AS c FROM crawl_runs").fetchone()["c"]

        assert likes_run["new_count"] == 1
        assert likes_run["updated_count"] == 0
        assert likes_run["removed_count"] == 0
        assert favorite_run_count == 0


if __name__ == "__main__":
    tests = [
        test_removed_favorite_reappearing_is_reactivated_not_inserted,
        test_suspicious_large_removed_set_aborts_without_marking_removed,
        test_large_removed_set_can_be_confirmed_explicitly,
        test_apply_like_crawl_writes_likes_without_touching_favorites,
        test_like_crawl_run_is_recorded_separately,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)

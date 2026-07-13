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
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")

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


def insert_favorite(
    conn,
    favorite_id: str,
    *,
    is_removed: int = 0,
    title: str = "old",
    user_note: str | None = None,
    category_id: int | None = None,
    favorited_at: str | None = None,
    video_created_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO favorites (
            id, title, first_seen_at, last_seen_at, raw_json, is_removed,
            discovery_index, user_note, category_id, favorited_at, video_created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            favorite_id,
            title,
            now,
            now,
            "{}",
            is_removed,
            1,
            user_note,
            category_id,
            favorited_at,
            video_created_at,
        ),
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


def test_repeated_favorite_sync_is_idempotent_and_preserves_local_fields() -> None:
    with isolated_sync_db() as conn:
        conn.execute(
            """
            INSERT INTO categories (
                account_id, name, auto_name, item_count, created_at, updated_at
            ) VALUES ('default', '本地分类', '本地分类', 1, '2026-01-01', '2026-01-01')
            """
        )
        category_id = conn.execute("SELECT id FROM categories").fetchone()["id"]
        insert_favorite(
            conn,
            "stable-1",
            title="old title",
            user_note="keep my note",
            category_id=category_id,
            favorited_at="2026-01-02 03:04:05",
            video_created_at="2025-12-01 00:00:00",
        )
        batch = [
            Favorite(
                id="stable-1",
                title="new title",
                author="new author",
                raw_json='{"fresh": true}',
                video_created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
        ]

        first = sync.apply_crawl(batch, is_first_crawl=False)
        second = sync.apply_crawl(batch, is_first_crawl=False)

        row = conn.execute(
            """
            SELECT title, author, user_note, category_id, favorited_at,
                   video_created_at, is_removed
            FROM favorites
            WHERE id = 'stable-1'
            """
        ).fetchone()
        assert first.new_count == 0
        assert second.new_count == 0
        assert conn.execute("SELECT COUNT(*) FROM favorites WHERE id = 'stable-1'").fetchone()[0] == 1
        assert row["title"] == "new title"
        assert row["author"] == "new author"
        assert row["user_note"] == "keep my note"
        assert row["category_id"] == category_id
        assert str(row["favorited_at"]) == "2026-01-02 03:04:05"
        assert str(row["video_created_at"]) == "2025-12-01 00:00:00"
        assert row["is_removed"] == 0


if __name__ == "__main__":
    tests = [
        test_removed_favorite_reappearing_is_reactivated_not_inserted,
        test_suspicious_large_removed_set_aborts_without_marking_removed,
        test_large_removed_set_can_be_confirmed_explicitly,
        test_apply_like_crawl_writes_likes_without_touching_favorites,
        test_like_crawl_run_is_recorded_separately,
        test_repeated_favorite_sync_is_idempotent_and_preserves_local_fields,
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

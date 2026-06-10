"""
Multi-tenant foundation tests.

Run:
    python tests/test_multitenant_foundation.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from src import db
from src.crawler import sync
from src.models import Favorite


@contextmanager
def isolated_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)

    original_sync_get_connection = sync.get_connection
    original_sync_transaction = sync.transaction

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
        sync.get_connection = original_sync_get_connection
        sync.transaction = original_sync_transaction
        conn.close()


def test_schema_has_private_cloud_foundation_tables() -> None:
    schema = db.SCHEMA_SQL

    assert "CREATE TABLE IF NOT EXISTS users" in schema
    assert "CREATE TABLE IF NOT EXISTS invite_codes" in schema
    assert "CREATE TABLE IF NOT EXISTS web_sessions" in schema
    assert "CREATE TABLE IF NOT EXISTS job_queue" in schema
    assert "user_id          TEXT NOT NULL DEFAULT 'default'" in schema
    assert "PRIMARY KEY (user_id, id)" in schema
    assert "user_id       TEXT NOT NULL DEFAULT 'default'" in schema


def test_crawl_sync_is_scoped_by_user_id_and_allows_same_aweme_per_user() -> None:
    with isolated_db() as conn:
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed, discovery_index
            ) VALUES ('bob', 'same-aweme', 'bob copy', '2026-05-26', '2026-05-26', 0, 0)
            """
        )

        result = sync.apply_crawl_for_kind(
            "favorites",
            [Favorite(id="same-aweme", title="alice copy")],
            is_first_crawl=True,
            user_id="alice",
        )

        rows = conn.execute(
            "SELECT user_id, id, title, is_removed FROM favorites ORDER BY user_id"
        ).fetchall()
        assert result.new_count == 1
        assert [(r["user_id"], r["id"], r["title"], r["is_removed"]) for r in rows] == [
            ("alice", "same-aweme", "alice copy", 0),
            ("bob", "same-aweme", "bob copy", 0),
        ]


def test_crawl_removal_only_marks_current_user_items_removed() -> None:
    with isolated_db() as conn:
        for user_id, item_id in [("alice", "keep"), ("alice", "gone"), ("bob", "gone")]:
            conn.execute(
                """
                INSERT INTO favorites (
                    user_id, id, title, first_seen_at, last_seen_at, is_removed, discovery_index
                ) VALUES (?, ?, ?, '2026-05-26', '2026-05-26', 0, 0)
                """,
                (user_id, item_id, f"{user_id} {item_id}"),
            )

        result = sync.apply_crawl_for_kind(
            "favorites",
            [Favorite(id="keep", title="alice keep")],
            is_first_crawl=False,
            user_id="alice",
        )

        rows = conn.execute(
            """
            SELECT user_id, id, is_removed
            FROM favorites
            ORDER BY user_id, id
            """
        ).fetchall()
        assert result.removed_count == 1
        assert [(r["user_id"], r["id"], r["is_removed"]) for r in rows] == [
            ("alice", "gone", 1),
            ("alice", "keep", 0),
            ("bob", "gone", 0),
        ]


if __name__ == "__main__":
    tests = [
        test_schema_has_private_cloud_foundation_tables,
        test_crawl_sync_is_scoped_by_user_id_and_allows_same_aweme_per_user,
        test_crawl_removal_only_marks_current_user_items_removed,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            print(f"FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)

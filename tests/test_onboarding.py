"""
Onboarding status tests.

Run:
    python tests/test_onboarding.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from src import db
from src import onboarding


@contextmanager
def isolated_onboarding_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY)")
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES ('default', '本地默认用户', '2026-06-11 00:00:00')
        """
    )

    original_get_connection = onboarding.get_connection
    onboarding.get_connection = lambda: conn
    try:
        yield conn
    finally:
        onboarding.get_connection = original_get_connection
        conn.close()


def test_empty_database_needs_setup() -> None:
    with isolated_onboarding_db():
        status = onboarding.get_onboarding_status("default")

        assert status["needs_setup"] is True
        assert status["has_any_items"] is False
        assert status["favorites"]["total"] == 0
        assert status["favorites"]["indexed"] == 0
        assert status["likes"]["total"] == 0
        assert status["likes"]["indexed"] == 0


def test_items_and_index_counts_are_content_kind_scoped() -> None:
    now = "2026-06-11 00:00:00"
    with isolated_onboarding_db() as conn:
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'fav-1', '收藏 1', ?, ?, 0)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO likes (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'like-1', '喜欢 1', ?, ?, 0)
            """,
            (now, now),
        )
        conn.execute("INSERT INTO favorites_vec (id) VALUES ('fav-1')")
        conn.execute("INSERT INTO likes_vec (id) VALUES ('default:like-1')")

        status = onboarding.get_onboarding_status("default")

        assert status["has_any_items"] is True
        assert status["needs_setup"] is False
        assert status["favorites"]["total"] == 1
        assert status["favorites"]["indexed"] == 1
        assert status["favorites"]["needs_index"] is False
        assert status["likes"]["total"] == 1
        assert status["likes"]["indexed"] == 1
        assert status["likes"]["needs_index"] is False


def test_profile_and_job_summary_are_reported() -> None:
    now = "2026-06-11 00:00:00"
    with isolated_onboarding_db() as conn:
        conn.execute(
            """
            UPDATE users
            SET douyin_nickname = '测试账号',
                douyin_unique_id = 'test-id',
                douyin_avatar_url = 'https://example.com/avatar.jpg',
                douyin_profile_updated_at = ?
            WHERE id = 'default'
            """,
            (now,),
        )
        for status in ("pending", "running", "failed", "success"):
            conn.execute(
                """
                INSERT INTO job_queue (user_id, kind, status, created_at)
                VALUES ('default', 'sync_favorites', ?, ?)
                """,
                (status, now),
            )

        status = onboarding.get_onboarding_status("default")

        assert status["has_profile"] is True
        assert status["profile"]["nickname"] == "测试账号"
        assert status["profile"]["unique_id"] == "test-id"
        assert status["jobs"]["pending"] == 1
        assert status["jobs"]["running"] == 1
        assert status["jobs"]["failed"] == 1
        assert status["jobs"]["success"] == 1
        assert status["jobs"]["needs_attention"] is True


if __name__ == "__main__":
    tests = [
        test_empty_database_needs_setup,
        test_items_and_index_counts_are_content_kind_scoped,
        test_profile_and_job_summary_are_reported,
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

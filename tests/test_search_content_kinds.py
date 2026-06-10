"""
Search content-kind tests.

Run:
    python tests/test_search_content_kinds.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.db import SCHEMA_SQL
from src.search import hybrid


@contextmanager
def isolated_search_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)

    original_get_connection = hybrid.get_connection

    def get_connection():
        return conn

    hybrid.get_connection = get_connection
    try:
        yield conn
    finally:
        hybrid.get_connection = original_get_connection
        conn.close()


def test_hydrate_can_read_likes_table() -> None:
    with isolated_search_db() as conn:
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO likes (
                id, title, author, liked_at, first_seen_at, last_seen_at,
                raw_json, is_removed, discovery_index, video_created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (
                "like-1",
                "liked title",
                "liked author",
                now,
                now,
                now,
                "{}",
                "2024-01-02 03:04:05+00:00",
            ),
        )

        hits = hybrid._hydrate([("like-1", 1.0, 1, None)], content_kind="likes")

        assert len(hits) == 1
        assert hits[0].id == "like-1"
        assert hits[0].title == "liked title"
        assert hits[0].author == "liked author"
        assert hits[0].favorited_at is not None
        assert hits[0].video_created_at == "2024-01-02 03:04:05+00:00"


if __name__ == "__main__":
    tests = [
        test_hydrate_can_read_likes_table,
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

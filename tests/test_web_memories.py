"""
Web memory-corner tests.

Run:
    python tests/test_web_memories.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.db import SCHEMA_SQL
from src.recall import selector
from src.web import app as web_app


@contextmanager
def isolated_memories_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, embedding BLOB)")

    original_web_get_connection = web_app.get_connection
    original_selector_get_connection = selector.get_connection
    web_app.get_connection = lambda: conn
    selector.get_connection = lambda: conn
    try:
        yield conn
    finally:
        web_app.get_connection = original_web_get_connection
        selector.get_connection = original_selector_get_connection
        conn.close()


def insert_memory_item(conn: sqlite3.Connection, item_id: str, title: str, created_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO favorites (
            id, title, author, video_url, cover_url, favorited_at,
            first_seen_at, last_seen_at, raw_json, is_removed, discovery_index,
            video_created_at, digg_count
        ) VALUES (?, ?, 'author', ?, NULL, ?, ?, ?, '{}', 0, 1, ?, 100)
        """,
        (
            item_id,
            title,
            f"https://example.test/{item_id}",
            now - timedelta(days=90),
            now - timedelta(days=90),
            now,
            created_at,
        ),
    )


def test_memories_page_shows_weekly_memory_corner() -> None:
    with isolated_memories_db() as conn:
        insert_memory_item(
            conn,
            "anniv",
            "去年这周的视频",
            datetime.now(timezone.utc) - timedelta(days=365),
        )
        client = TestClient(web_app.app)

        response = client.get("/memories")

        assert response.status_code == 200
        assert "本周回忆角" in response.text
        assert "去年这周的视频" in response.text


if __name__ == "__main__":
    tests = [test_memories_page_shows_weekly_memory_corner]
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

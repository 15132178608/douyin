"""
Indexer content-kind tests.

Run:
    python tests/test_indexer_content_kinds.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.db import SCHEMA_SQL
from src.embedding import indexer


@contextmanager
def isolated_index_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, embedding BLOB)")

    original_get_connection = indexer.get_connection

    def get_connection():
        return conn

    indexer.get_connection = get_connection
    try:
        yield conn
    finally:
        indexer.get_connection = original_get_connection
        conn.close()


def insert_item(conn: sqlite3.Connection, table: str, item_id: str, discovery_index: int) -> None:
    now = datetime.now(timezone.utc)
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, {time_column}, first_seen_at, last_seen_at,
            raw_json, is_removed, discovery_index
        ) VALUES (?, ?, NULL, ?, ?, ?, 0, ?)
        """,
        (item_id, item_id, now, now, "{}", discovery_index),
    )


def test_find_unindexed_ids_can_target_likes_table() -> None:
    with isolated_index_db() as conn:
        insert_item(conn, "favorites", "favorite-1", 1)
        insert_item(conn, "likes", "like-1", 2)

        assert indexer.find_unindexed_ids(content_kind="likes") == ["like-1"]
        assert indexer.find_unindexed_ids(content_kind="favorites") == ["favorite-1"]


def test_fetch_rows_by_ids_can_target_likes_table() -> None:
    with isolated_index_db() as conn:
        insert_item(conn, "likes", "like-1", 1)

        rows = indexer._fetch_rows_by_ids(["like-1"], content_kind="likes")

        assert rows[0]["id"] == "like-1"
        assert rows[0]["title"] == "like-1"


if __name__ == "__main__":
    tests = [
        test_find_unindexed_ids_can_target_likes_table,
        test_fetch_rows_by_ids_can_target_likes_table,
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

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
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")
    for table in ("favorites_fts", "likes_fts"):
        conn.execute(
            f"""
            CREATE TABLE {table} (
                id TEXT,
                user_id TEXT,
                title TEXT,
                description TEXT,
                author TEXT,
                user_note TEXT
            )
            """
        )

    original_get_connection = indexer.get_connection
    original_transaction = indexer.transaction

    def get_connection():
        return conn

    indexer.get_connection = get_connection

    @contextmanager
    def transaction():
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    indexer.transaction = transaction
    try:
        yield conn
    finally:
        indexer.get_connection = original_get_connection
        indexer.transaction = original_transaction
        conn.close()


def insert_item(
    conn: sqlite3.Connection,
    table: str,
    item_id: str,
    discovery_index: int,
    *,
    user_id: str = "default",
) -> None:
    now = datetime.now(timezone.utc)
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, user_id, now),
    )
    conn.execute(
        f"""
        INSERT INTO {table} (
            user_id, id, title, {time_column}, first_seen_at, last_seen_at,
            raw_json, is_removed, discovery_index
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, 0, ?)
        """,
        (user_id, item_id, item_id, now, now, "{}", discovery_index),
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


def test_force_index_replaces_only_the_requested_user_partition() -> None:
    class FakeEncoder:
        def encode(self, texts, batch_size=32):
            import numpy as np

            return np.ones((len(texts), 1024), dtype=np.float32)

    with isolated_index_db() as conn:
        insert_item(conn, "favorites", "active", 1, user_id="alice")
        insert_item(conn, "favorites", "removed", 2, user_id="alice")
        insert_item(conn, "favorites", "bob-active", 1, user_id="bob")
        conn.execute(
            "UPDATE favorites SET is_removed = 1 WHERE user_id = 'alice' AND id = 'removed'"
        )
        for user_id, index_id in (
            ("alice", "alice:stale"),
            ("alice", "alice:removed"),
            ("bob", "bob:sentinel"),
        ):
            conn.execute(
                "INSERT INTO favorites_vec (id, user_id, embedding) VALUES (?, ?, ?)",
                (index_id, user_id, b"old"),
            )
            conn.execute(
                """
                INSERT INTO favorites_fts (
                    id, user_id, title, description, author, user_note
                ) VALUES (?, ?, 'old', '', '', '')
                """,
                (index_id, user_id),
            )
        original_get_encoder = indexer.get_encoder
        indexer.get_encoder = lambda: FakeEncoder()
        try:
            result = indexer.index_all(force=True, user_id="alice")
        finally:
            indexer.get_encoder = original_get_encoder

        assert result["indexed"] == 1
        assert {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM favorites_vec WHERE user_id = 'alice'"
            ).fetchall()
        } == {"alice:active"}
        assert {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM favorites_fts WHERE user_id = 'alice'"
            ).fetchall()
        } == {"alice:active"}
        assert conn.execute(
            "SELECT id FROM favorites_vec WHERE user_id = 'bob'"
        ).fetchone()["id"] == "bob:sentinel"
        assert conn.execute(
            "SELECT id FROM favorites_fts WHERE user_id = 'bob'"
        ).fetchone()["id"] == "bob:sentinel"

        conn.execute(
            "UPDATE favorites SET is_removed = 1 WHERE user_id = 'alice' AND id = 'active'"
        )
        empty_result = indexer.index_all(force=True, user_id="alice")

        assert empty_result == {"indexed": 0, "total_in_db": 0}
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM favorites_vec WHERE user_id = 'alice'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM favorites_fts WHERE user_id = 'alice'"
        ).fetchone()["c"] == 0


def test_force_index_failure_preserves_existing_search_partition() -> None:
    class FailingEncoder:
        def encode(self, texts, batch_size=32):
            raise RuntimeError("model unavailable")

    with isolated_index_db() as conn:
        insert_item(conn, "favorites", "active", 1, user_id="alice")
        for index_id in ("alice:active", "alice:stale"):
            conn.execute(
                "INSERT INTO favorites_vec (id, user_id, embedding) VALUES (?, 'alice', ?)",
                (index_id, b"old"),
            )
            conn.execute(
                """
                INSERT INTO favorites_fts (
                    id, user_id, title, description, author, user_note
                ) VALUES (?, 'alice', 'old', '', '', '')
                """,
                (index_id,),
            )
        original_get_encoder = indexer.get_encoder
        indexer.get_encoder = lambda: FailingEncoder()
        try:
            try:
                indexer.index_all(force=True, user_id="alice")
            except RuntimeError as exc:
                assert "model unavailable" in str(exc)
            else:
                raise AssertionError("force indexing should surface encoder failure")
        finally:
            indexer.get_encoder = original_get_encoder

        assert {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM favorites_vec WHERE user_id = 'alice'"
            ).fetchall()
        } == {"alice:active", "alice:stale"}
        assert {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM favorites_fts WHERE user_id = 'alice'"
            ).fetchall()
        } == {"alice:active", "alice:stale"}


if __name__ == "__main__":
    tests = [
        test_find_unindexed_ids_can_target_likes_table,
        test_fetch_rows_by_ids_can_target_likes_table,
        test_force_index_replaces_only_the_requested_user_partition,
        test_force_index_failure_preserves_existing_search_partition,
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

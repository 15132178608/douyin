"""
Search content-kind tests.

Run:
    python tests/test_search_content_kinds.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlite_vec
import numpy as np

from src.config import settings
from src.db import SCHEMA_SQL
from src import db, jobs
from src.embedding import indexer
from src.search import hybrid
from src.tenancy import scoped_item_id


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


@contextmanager
def isolated_partitioned_search_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        """
        CREATE VIRTUAL TABLE favorites_fts USING fts5(
            id UNINDEXED,
            user_id UNINDEXED,
            title,
            description,
            author,
            user_note,
            tokenize = 'unicode61'
        )
        """
    )

    original_get_connection = hybrid.get_connection
    original_vec_search = hybrid._vec_search
    hybrid.get_connection = lambda: conn
    hybrid._vec_search = lambda *args, **kwargs: []
    try:
        yield conn
    finally:
        hybrid.get_connection = original_get_connection
        hybrid._vec_search = original_vec_search
        conn.close()


def insert_search_item(conn: sqlite3.Connection, user_id: str, item_id: str, title: str) -> None:
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, '2026-07-07 00:00:00')
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, user_id),
    )
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, first_seen_at, last_seen_at, is_removed
        ) VALUES (?, ?, ?, '2026-07-07', '2026-07-07', 0)
        """,
        (user_id, item_id, title),
    )
    conn.execute(
        """
        INSERT INTO favorites_fts (id, user_id, title, description, author, user_note)
        VALUES (?, ?, ?, '', '', '')
        """,
        (scoped_item_id(user_id, item_id), user_id, title),
    )


def test_search_filters_fts_by_physical_user_partition_before_limit() -> None:
    with isolated_partitioned_search_db() as conn:
        for index in range(300):
            insert_search_item(conn, "alice", f"a-{index:03d}", "banana shared topic")
        for index in range(5):
            insert_search_item(conn, "bob", f"b-{index:03d}", "banana shared topic")

        hits = hybrid.search_for_kind("banana", top_k=10, content_kind="favorites", user_id="bob")

        assert [hit.id for hit in hits] == [f"b-{index:03d}" for index in range(5)]


def test_search_results_do_not_cross_user_partitions() -> None:
    with isolated_partitioned_search_db() as conn:
        insert_search_item(conn, "alice", "a-1", "private banana note")
        insert_search_item(conn, "bob", "b-1", "private banana note")

        hits = hybrid.search_for_kind("banana", top_k=10, content_kind="favorites", user_id="alice")

        assert [hit.id for hit in hits] == ["a-1"]


def test_init_schema_rebuilds_legacy_search_index_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES ('default', 'Default', '2026-07-07 00:00:00'),
               ('alice', 'Alice', '2026-07-07 00:00:00')
        """
    )
    for user_id in ("default", "alice"):
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES (?, ?, ?, '2026-07-07', '2026-07-07', 0)
            """,
            (user_id, f"{user_id}-favorite", f"banana {user_id} favorite recovery"),
        )
        conn.execute(
            """
            INSERT INTO likes (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES (?, ?, ?, '2026-07-07', '2026-07-07', 0)
            """,
            (user_id, f"{user_id}-like", f"banana {user_id} like recovery"),
        )
    conn.execute(
        """
        CREATE VIRTUAL TABLE favorites_vec USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[1024]
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE favorites_fts USING fts5(
            id UNINDEXED,
            title,
            description,
            author,
            user_note,
            tokenize = 'unicode61'
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE likes_vec USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[1024]
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE likes_fts USING fts5(
            id UNINDEXED,
            title,
            description,
            author,
            user_note,
            tokenize = 'unicode61'
        )
        """
    )
    conn.close()

    original_path = settings.db_path
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        db.init_schema()
        live = db.get_connection()
        vector_columns = {row["name"] for row in live.execute("PRAGMA table_info(favorites_vec)").fetchall()}
        fts_columns = {row["name"] for row in live.execute("PRAGMA table_info(favorites_fts)").fetchall()}
        monkeypatch.setattr(hybrid, "_vec_search", lambda *args, **kwargs: [])

        assert "user_id" in vector_columns
        assert "user_id" in fts_columns
        assert hybrid.search_for_kind("banana", user_id="default") == []
        pending = db.list_pending_search_reindexes()
        assert {
            (item["user_id"], item["content_kind"], item["reason"])
            for item in pending
        } == {
            ("default", "favorites", "search_index_schema_rebuilt"),
            ("default", "likes", "search_index_schema_rebuilt"),
            ("alice", "favorites", "search_index_schema_rebuilt"),
            ("alice", "likes", "search_index_schema_rebuilt"),
        }

        class FakeEncoder:
            def encode(self, texts, batch_size=32):
                return np.ones((len(texts), 1024), dtype=np.float32)

        monkeypatch.setattr(indexer, "get_encoder", lambda: FakeEncoder())
        queued = jobs.enqueue_pending_search_reindexes()
        assert len(queued) == 4
        while jobs.run_next_job():
            pass
        assert db.list_pending_search_reindexes() == []
        for user_id in ("default", "alice"):
            assert [hit.id for hit in hybrid.search_for_kind("banana", user_id=user_id)] == [
                f"{user_id}-favorite"
            ]
            assert [
                hit.id
                for hit in hybrid.search_for_kind(
                    "banana", content_kind="likes", user_id=user_id
                )
            ] == [f"{user_id}-like"]
        assert jobs.enqueue_pending_search_reindexes() == []
    finally:
        db.close_connection()
        monkeypatch.setattr(settings, "db_path", original_path)


if __name__ == "__main__":
    tests = [
        test_hydrate_can_read_likes_table,
        test_search_filters_fts_by_physical_user_partition_before_limit,
        test_search_results_do_not_cross_user_partitions,
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

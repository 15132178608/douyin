"""
Data export and backup tests.

Run:
    python tests/test_exporter.py
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from src import db
from src import exporter


@contextmanager
def isolated_export_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")
    conn.execute(
        "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
    )
    conn.execute(
        "INSERT INTO users (id, display_name, created_at) VALUES ('bob', 'Bob', '2026-05-26 00:00:00')"
    )

    original_export_get_connection = exporter.get_connection
    exporter.get_connection = lambda: conn
    try:
        yield conn
    finally:
        exporter.get_connection = original_export_get_connection
        conn.close()


def insert_item(
    conn: sqlite3.Connection,
    table: str,
    user_id: str,
    item_id: str,
    title: str,
    *,
    is_removed: int = 0,
    note: str | None = None,
) -> None:
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            user_id, id, title, author, video_url, cover_url, user_note, raw_json,
            {time_column}, first_seen_at, last_seen_at, is_removed, discovery_index
        ) VALUES (?, ?, ?, 'author', ?, NULL, ?, '{{}}',
                  '2026-05-26 12:00:00', '2026-05-26 12:00:00',
                  '2026-05-26 12:00:00', ?, 1)
        """,
        (user_id, item_id, title, f"https://example.test/{item_id}", note, is_removed),
    )


def test_json_export_is_user_scoped_and_omits_removed_items() -> None:
    with isolated_export_db() as conn, TemporaryDirectory() as tmp:
        insert_item(conn, "favorites", "alice", "a1", "alice keep", note="good")
        insert_item(conn, "favorites", "alice", "a2", "alice removed", is_removed=1)
        insert_item(conn, "favorites", "bob", "b1", "bob private")

        result = exporter.export_json(Path(tmp), user_id="alice", content_kind="favorites")

        data = json.loads(result.path.read_text(encoding="utf-8"))
        assert result.count == 1
        assert data["user_id"] == "alice"
        assert data["content_kind"] == "favorites"
        assert [item["id"] for item in data["items"]] == ["a1"]
        assert data["items"][0]["user_note"] == "good"


def test_markdown_export_writes_readable_item_list() -> None:
    with isolated_export_db() as conn, TemporaryDirectory() as tmp:
        insert_item(conn, "likes", "alice", "l1", "liked item", note="remember this")

        result = exporter.export_markdown(Path(tmp), user_id="alice", content_kind="likes")

        text = result.path.read_text(encoding="utf-8")
        assert result.count == 1
        assert "# 抖音喜欢导出" in text
        assert "## liked item" in text
        assert "remember this" in text
        assert "https://example.test/l1" in text


def test_sqlite_backup_creates_readable_database_copy() -> None:
    with isolated_export_db() as conn, TemporaryDirectory() as tmp:
        insert_item(conn, "favorites", "alice", "a1", "alice keep")

        result = exporter.backup_sqlite(Path(tmp))

        copied = sqlite3.connect(result.path)
        try:
            count = copied.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        finally:
            copied.close()
        assert result.count == 1
        assert count == 1


if __name__ == "__main__":
    tests = [
        test_json_export_is_user_scoped_and_omits_removed_items,
        test_markdown_export_writes_readable_item_list,
        test_sqlite_backup_creates_readable_database_copy,
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

"""
Category listing tests.

Run:
    python tests/test_categories.py
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.categorize import cluster
from src.db import SCHEMA_SQL


@contextmanager
def isolated_category_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")

    original_get_connection = cluster.get_connection

    def get_connection():
        return conn

    cluster.get_connection = get_connection
    try:
        yield conn
    finally:
        cluster.get_connection = original_get_connection
        conn.close()


def insert_category(
    conn: sqlite3.Connection,
    name: str,
    *,
    item_count: int,
    keywords: list[str] | None = None,
) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO categories (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "default",
            name,
            name,
            json.dumps(keywords or [], ensure_ascii=False),
            item_count,
            "kmeans",
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_like_category(
    conn: sqlite3.Connection,
    name: str,
    *,
    item_count: int,
    keywords: list[str] | None = None,
) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO like_categories (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "default",
            name,
            name,
            json.dumps(keywords or [], ensure_ascii=False),
            item_count,
            "kmeans",
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_favorite(
    conn: sqlite3.Connection,
    favorite_id: str,
    *,
    category_id: int,
    is_removed: int,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO favorites (
            id, title, first_seen_at, last_seen_at, raw_json, is_removed, category_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (favorite_id, favorite_id, now, now, "{}", is_removed, category_id),
    )


def insert_like(
    conn: sqlite3.Connection,
    like_id: str,
    *,
    category_id: int,
    is_removed: int,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO likes (
            id, title, first_seen_at, last_seen_at, raw_json, is_removed, category_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (like_id, like_id, now, now, "{}", is_removed, category_id),
    )


def test_list_categories_counts_only_active_favorites_and_hides_empty_categories() -> None:
    with isolated_category_db() as conn:
        active_category = insert_category(conn, "active cached two", item_count=2)
        empty_category = insert_category(conn, "empty cached one", item_count=1)
        insert_favorite(conn, "active-1", category_id=active_category, is_removed=0)
        insert_favorite(conn, "removed-1", category_id=active_category, is_removed=1)
        insert_favorite(conn, "removed-2", category_id=empty_category, is_removed=1)

        categories = cluster.list_categories()

        assert [c["id"] for c in categories] == [active_category]
        assert categories[0]["item_count"] == 1


def test_list_like_categories_uses_likes_tables_only() -> None:
    with isolated_category_db() as conn:
        favorite_category = insert_category(conn, "favorite category", item_count=1)
        like_category = insert_like_category(conn, "like category", item_count=2)
        insert_favorite(conn, "favorite-1", category_id=favorite_category, is_removed=0)
        insert_like(conn, "like-1", category_id=like_category, is_removed=0)
        insert_like(conn, "like-2", category_id=like_category, is_removed=1)

        categories = cluster.list_categories(content_kind="likes")

        assert [c["id"] for c in categories] == [like_category]
        assert categories[0]["name"] == "like category"
        assert categories[0]["item_count"] == 1
        assert cluster.count_uncategorized(content_kind="likes") == 0


def test_merge_categories_moves_items_and_removes_source_category() -> None:
    with isolated_category_db() as conn:
        target = insert_category(conn, "target", item_count=1)
        source = insert_category(conn, "source", item_count=1)
        insert_favorite(conn, "target-1", category_id=target, is_removed=0)
        insert_favorite(conn, "source-1", category_id=source, is_removed=0)

        assert cluster.merge_categories(target, source) is True

        item_categories = {
            r["id"]: r["category_id"]
            for r in conn.execute("SELECT id, category_id FROM favorites ORDER BY id").fetchall()
        }
        source_row = conn.execute("SELECT id FROM categories WHERE id = ?", (source,)).fetchone()
        target_count = conn.execute("SELECT item_count FROM categories WHERE id = ?", (target,)).fetchone()["item_count"]
        assert item_categories == {"source-1": target, "target-1": target}
        assert source_row is None
        assert target_count == 2


def test_move_item_to_category_supports_uncategorized_bucket_and_recounts() -> None:
    with isolated_category_db() as conn:
        old_cat = insert_category(conn, "old", item_count=1)
        new_cat = insert_category(conn, "new", item_count=0)
        insert_favorite(conn, "item-1", category_id=old_cat, is_removed=0)

        assert cluster.move_item_to_category("item-1", new_cat) is True
        assert cluster.move_item_to_category("item-1", None) is True

        item = conn.execute("SELECT category_id FROM favorites WHERE id = 'item-1'").fetchone()
        counts = {
            r["name"]: r["item_count"]
            for r in conn.execute("SELECT name, item_count FROM categories ORDER BY name").fetchall()
        }
        assert item["category_id"] is None
        assert counts == {"new": 0, "old": 0}


if __name__ == "__main__":
    tests = [
        test_list_categories_counts_only_active_favorites_and_hides_empty_categories,
        test_list_like_categories_uses_likes_tables_only,
        test_merge_categories_moves_items_and_removes_source_category,
        test_move_item_to_category_supports_uncategorized_bucket_and_recounts,
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

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.db import SCHEMA_SQL


def make_category_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")
    return conn


def insert_favorite(conn: sqlite3.Connection, item_id: str, category_id: int | None = None) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, first_seen_at, last_seen_at, raw_json, is_removed, category_id
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        ("default", item_id, item_id, now, now, "{}", category_id),
    )


def insert_category(conn: sqlite3.Connection, name: str) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO categories (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
        """,
        ("default", name, name, json.dumps([name], ensure_ascii=False), "kmeans", now, now),
    )
    return int(cur.lastrowid)


def test_import_categories_from_existing_database_matches_current_items_only(tmp_path: Path) -> None:
    from src import category_import

    current_path = tmp_path / "current.db"
    source_path = tmp_path / "old.db"
    current = make_category_db(current_path)
    source = make_category_db(source_path)
    try:
        for item_id in ("fav-a", "fav-b", "fav-c"):
            insert_favorite(current, item_id)
        travel = insert_category(source, "旅行")
        food = insert_category(source, "美食")
        insert_favorite(source, "fav-a", category_id=travel)
        insert_favorite(source, "fav-b", category_id=food)
        insert_favorite(source, "old-only", category_id=travel)

        result = category_import.import_categories_from_database(
            source_path,
            current_conn=current,
            current_db_path=current_path,
            content_kind="favorites",
        )

        assert result.imported is True
        assert result.category_count == 2
        assert result.assigned_item_count == 2
        categories = {
            row["name"]: row["item_count"]
            for row in current.execute("SELECT name, item_count FROM categories ORDER BY name")
        }
        assert categories == {"旅行": 1, "美食": 1}
        assignments = {
            row["id"]: row["category_id"]
            for row in current.execute("SELECT id, category_id FROM favorites ORDER BY id")
        }
        assert assignments["fav-a"] is not None
        assert assignments["fav-b"] is not None
        assert assignments["fav-c"] is None
    finally:
        current.close()
        source.close()


def test_category_import_refuses_to_overwrite_existing_categories(tmp_path: Path) -> None:
    from src import category_import

    current_path = tmp_path / "current.db"
    source_path = tmp_path / "old.db"
    current = make_category_db(current_path)
    source = make_category_db(source_path)
    try:
        insert_category(current, "已有分类")
        source_cat = insert_category(source, "旧分类")
        insert_favorite(current, "fav-a")
        insert_favorite(source, "fav-a", category_id=source_cat)

        result = category_import.import_categories_from_database(
            source_path,
            current_conn=current,
            current_db_path=current_path,
            content_kind="favorites",
        )

        assert result.imported is False
        assert result.reason == "current_has_categories"
        assert current.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 1
        assert current.execute("SELECT category_id FROM favorites WHERE id = 'fav-a'").fetchone()[0] is None
    finally:
        current.close()
        source.close()


def test_find_category_import_candidates_scores_matching_old_database(tmp_path: Path) -> None:
    from src import category_import

    current_path = tmp_path / "current.db"
    source_path = tmp_path / "old.db"
    current = make_category_db(current_path)
    source = make_category_db(source_path)
    try:
        insert_favorite(current, "fav-a")
        insert_favorite(current, "fav-b")
        cat_id = insert_category(source, "旧分类")
        insert_favorite(source, "fav-a", category_id=cat_id)
        insert_favorite(source, "not-current", category_id=cat_id)

        candidates = category_import.find_category_import_candidates(
            current_conn=current,
            current_db_path=current_path,
            search_paths=[source_path],
            content_kind="favorites",
        )

        assert len(candidates) == 1
        assert candidates[0].path == source_path
        assert candidates[0].category_count == 1
        assert candidates[0].match_count == 1
        assert candidates[0].source_item_count == 2
    finally:
        current.close()
        source.close()


def test_default_category_source_paths_discovers_old_install_database(tmp_path: Path) -> None:
    from src import category_import

    current_path = tmp_path / "current" / "data" / "recall.db"
    old_path = tmp_path / "old-install" / "DouyinRecall" / "data" / "recall.db"
    current_path.parent.mkdir(parents=True)
    old_path.parent.mkdir(parents=True)
    current_path.write_bytes(b"current")
    old_path.write_bytes(b"old")
    original_patterns = category_import._candidate_patterns
    category_import._candidate_patterns = lambda: [
        str(tmp_path / "*" / "DouyinRecall" / "data" / "recall.db"),
        str(current_path),
    ]
    try:
        paths = category_import.default_category_source_paths(current_path)
    finally:
        category_import._candidate_patterns = original_patterns

    assert paths == [old_path]

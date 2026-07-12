"""
Database schema tests.

Run:
    python tests/test_db_schema.py
"""
from __future__ import annotations

from src import db


def test_schema_defines_independent_likes_module_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS likes" in db.SCHEMA_SQL
    assert "liked_at" in db.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS like_categories" in db.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS like_crawl_runs" in db.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS unlike_log" in db.SCHEMA_SQL
    assert "FOREIGN KEY (user_id, like_id) REFERENCES likes(user_id, id)" in db.SCHEMA_SQL
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS likes_vec" in db.VEC_SCHEMA_SQL
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS likes_fts" in db.FTS_SCHEMA_SQL
    assert "user_id TEXT partition key" in db.VEC_SCHEMA_SQL
    assert "user_id UNINDEXED" in db.FTS_SCHEMA_SQL


def test_migration_defines_web_query_performance_indexes() -> None:
    source = db._migrate_schema.__code__.co_consts
    joined = "\n".join(str(item) for item in source)
    assert "idx_fav_active_order" in joined
    assert "idx_like_active_order" in joined
    assert "idx_fav_active_category_order" in joined
    assert "idx_like_active_category_order" in joined
    assert "idx_fav_active_author_order" in joined
    assert "idx_like_active_author_order" in joined


if __name__ == "__main__":
    tests = [
        test_schema_defines_independent_likes_module_tables,
        test_migration_defines_web_query_performance_indexes,
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

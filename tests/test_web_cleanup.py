"""
Web organizing, batch cleanup, and duplicate view tests.

Run:
    python tests/test_web_cleanup.py
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src import accounts, jobs
from src.categorize import cluster
from src.db import SCHEMA_SQL
from src.web import app as web_app
from src.web import helpers as web_helpers


@contextmanager
def isolated_cleanup_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")

    original_web_get_connection = web_helpers._db_get_connection
    original_accounts_get_connection = accounts.get_connection
    original_cluster_get_connection = cluster.get_connection
    original_jobs_get_connection = jobs.get_connection
    web_helpers._db_get_connection = lambda: conn
    accounts.get_connection = lambda: conn
    cluster.get_connection = lambda: conn
    jobs.get_connection = lambda: conn
    try:
        yield conn
    finally:
        web_helpers._db_get_connection = original_web_get_connection
        accounts.get_connection = original_accounts_get_connection
        cluster.get_connection = original_cluster_get_connection
        jobs.get_connection = original_jobs_get_connection
        conn.close()


def insert_item(conn: sqlite3.Connection, table: str, item_id: str, title: str) -> None:
    now = datetime.now(timezone.utc)
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, author, video_url, cover_url, user_note, raw_json,
            {time_column}, first_seen_at, last_seen_at, is_removed, discovery_index
        ) VALUES (?, ?, 'author', ?, NULL, NULL, '{{}}', ?, ?, ?, 0, 1)
        """,
        (item_id, title, f"https://example.test/{item_id}", now, now, now),
    )


def insert_category(conn: sqlite3.Connection, table: str, name: str) -> int:
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        f"""
        INSERT INTO {table} (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES ('default', ?, ?, '[]', 0, 'kmeans', ?, ?)
        """,
        (name, name, now, now),
    )
    return int(cur.lastrowid)


def test_card_category_move_route_updates_item_category() -> None:
    with isolated_cleanup_db() as conn:
        insert_item(conn, "favorites", "a1", "move me")
        cat_id = insert_category(conn, "categories", "target")
        client = TestClient(web_app.app)

        response = client.post("/favorites/a1/category", data={"category_id": str(cat_id)})

        row = conn.execute("SELECT category_id FROM favorites WHERE id = 'a1'").fetchone()
        assert response.status_code == 200
        assert row["category_id"] == cat_id
        assert "move me" in response.text


def test_batch_uncollect_route_marks_selected_items_and_enqueues_jobs() -> None:
    with isolated_cleanup_db() as conn:
        insert_item(conn, "favorites", "a1", "remove one")
        insert_item(conn, "favorites", "a2", "remove two")
        insert_item(conn, "favorites", "keep", "keep")
        client = TestClient(web_app.app)

        response = client.post("/favorites/batch/uncollect", data={"ids": ["a1", "a2"]})

        states = {
            r["id"]: r["is_removed"]
            for r in conn.execute("SELECT id, is_removed FROM favorites ORDER BY id").fetchall()
        }
        queued = conn.execute("SELECT COUNT(*) AS c FROM job_queue WHERE kind = 'uncollect'").fetchone()["c"]
        assert response.status_code == 200
        assert states == {"a1": 1, "a2": 1, "keep": 0}
        assert queued == 2
        assert "已加入后台队列" in response.text


def test_batch_export_route_downloads_selected_metadata() -> None:
    with isolated_cleanup_db() as conn:
        insert_item(conn, "favorites", "a1", "export one")
        insert_item(conn, "favorites", "a2", "export two")
        insert_item(conn, "favorites", "skip", "not selected")
        client = TestClient(web_app.app)

        response = client.post("/favorites/batch/export", data={"ids": ["a2", "a1", "missing"]})

        payload = json.loads(response.text)
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment;")
        assert "douyin-favorites-selected" in response.headers["content-disposition"]
        assert payload["content_kind"] == "favorites"
        assert payload["count"] == 2
        assert [item["id"] for item in payload["items"]] == ["a2", "a1"]
        assert [item["title"] for item in payload["items"]] == ["export two", "export one"]


def test_duplicates_page_shows_same_aweme_in_favorites_and_likes() -> None:
    with isolated_cleanup_db() as conn:
        insert_item(conn, "favorites", "same", "favorite copy")
        insert_item(conn, "likes", "same", "like copy")
        insert_item(conn, "likes", "only-like", "not duplicate")
        client = TestClient(web_app.app)

        response = client.get("/duplicates")

        assert response.status_code == 200
        assert "重合视频" in response.text
        assert '<div class="grid">' in response.text
        assert 'id="card-same"' in response.text
        assert "favorite copy" in response.text
        assert "not duplicate" not in response.text


if __name__ == "__main__":
    tests = [
        test_card_category_move_route_updates_item_category,
        test_batch_uncollect_route_marks_selected_items_and_enqueues_jobs,
        test_batch_export_route_downloads_selected_metadata,
        test_duplicates_page_shows_same_aweme_in_favorites_and_likes,
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

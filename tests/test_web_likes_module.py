"""
Web likes-module routing tests.

Run:
    python tests/test_web_likes_module.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.categorize import cluster
from src import jobs
from src import onboarding
from src.db import SCHEMA_SQL
from src.embedding import indexer
from src.web import app as web_app


@contextmanager
def isolated_web_db():
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
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, embedding BLOB)")

    original_web_get_connection = web_app.get_connection
    original_cluster_get_connection = cluster.get_connection
    original_jobs_get_connection = jobs.get_connection
    original_onboarding_get_connection = onboarding.get_connection
    original_index_one = indexer.index_one

    def get_connection():
        return conn

    web_app.get_connection = get_connection
    cluster.get_connection = get_connection
    jobs.get_connection = get_connection
    onboarding.get_connection = get_connection
    indexer.index_one = lambda *args, **kwargs: None
    try:
        yield conn
    finally:
        web_app.get_connection = original_web_get_connection
        cluster.get_connection = original_cluster_get_connection
        jobs.get_connection = original_jobs_get_connection
        onboarding.get_connection = original_onboarding_get_connection
        indexer.index_one = original_index_one
        conn.close()


def insert_item(conn: sqlite3.Connection, table: str, item_id: str, title: str, author: str) -> None:
    now = datetime.now(timezone.utc)
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, author, video_url, cover_url, user_note,
            raw_json, {time_column}, first_seen_at, last_seen_at,
            is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 0, ?)
        """,
        (
            item_id,
            title,
            author,
            f"https://example.test/video/{item_id}",
            "{}",
            now,
            now,
            now,
            1,
        ),
    )


def insert_like_with_publish_time(
    conn: sqlite3.Connection,
    item_id: str,
    title: str,
    publish_time: str,
) -> None:
    now = "2026-05-26 12:00:00"
    conn.execute(
        """
        INSERT INTO likes (
            id, title, author, video_url, cover_url, user_note,
            raw_json, liked_at, first_seen_at, last_seen_at,
            is_removed, discovery_index, video_created_at
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, 0, ?, ?)
        """,
        (
            item_id,
            title,
            "publish author",
            f"https://example.test/video/{item_id}",
            "{}",
            now,
            now,
            1,
            publish_time,
        ),
    )


def insert_item_for_paging(conn: sqlite3.Connection, table: str, index: int) -> None:
    timestamp = "2026-05-26 12:00:00"
    item_id = f"{table}-page-{index:03d}"
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, author, video_url, cover_url, user_note,
            raw_json, {time_column}, first_seen_at, last_seen_at,
            is_removed, discovery_index, video_created_at
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, 0, ?, ?)
        """,
        (
            item_id,
            f"paged {table} {index:03d}",
            "paging author",
            f"https://example.test/video/{item_id}",
            "{}",
            timestamp,
            timestamp,
            index,
            timestamp,
        ),
    )


def insert_category(conn: sqlite3.Connection, table: str, name: str) -> int:
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        f"""
        INSERT INTO {table} (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES ('default', ?, ?, '[]', 1, 'kmeans', ?, ?)
        """,
        (name, name, now, now),
    )
    return int(cur.lastrowid)


def test_likes_home_reads_likes_table_and_hides_uncollect_action() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "fav-1", "favorite only", "fav author")
        insert_item(conn, "likes", "like-1", "liked only", "like author")
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert "liked only" in response.text
        assert "favorite only" not in response.text
        assert "/likes/track/open/like-1" in response.text
        assert "/likes/like-1/unlike" in response.text
        assert "/favorites/like-1/uncollect" not in response.text
        assert "1 条喜欢" in response.text


def test_likes_home_uses_video_publish_time_when_like_time_missing() -> None:
    with isolated_web_db() as conn:
        insert_like_with_publish_time(
            conn,
            "like-published",
            "published time item",
            "2024-01-02 03:04:05+00:00",
        )
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert "published time item" in response.text
        assert "发布于 2024-01-02" in response.text
        assert "发现于" not in response.text
        assert "时间未知" not in response.text


def test_likes_home_supports_mobile_load_more_and_desktop_pagination() -> None:
    with isolated_web_db() as conn:
        page_size = web_app.HOME_PAGE_SIZE
        for index in range(page_size + 2):
            insert_item_for_paging(conn, "likes", index)
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert f"paged likes {page_size + 1:03d}" in response.text
        assert "paged likes 001" not in response.text
        assert "load-more-sentinel" in response.text
        assert f"/likes/page?offset={page_size}" in response.text
        assert 'data-load-more-url="/likes/page?offset=' in response.text
        assert 'hx-trigger="revealed"' not in response.text
        assert "pagination-desktop" in response.text
        assert "/likes?p=2" in response.text

        next_page = client.get(f"/likes/page?offset={page_size}")

        assert next_page.status_code == 200
        assert "paged likes 001" in next_page.text
        assert "paged likes 000" in next_page.text
        assert "load-more-sentinel" not in next_page.text

        desktop_page = client.get("/likes?p=2")

        assert desktop_page.status_code == 200
        assert "paged likes 001" in desktop_page.text
        assert "paged likes 000" in desktop_page.text
        assert "第 2 / 2 页" in desktop_page.text
        assert "/likes?p=1" in desktop_page.text


def test_likes_home_preserves_custom_page_size_for_full_desktop_rows() -> None:
    with isolated_web_db() as conn:
        for index in range(26):
            insert_item_for_paging(conn, "likes", index)
        client = TestClient(web_app.app)

        response = client.get("/likes?page_size=24")

        assert response.status_code == 200
        assert response.text.count('class="card"') == 24
        assert "paged likes 025" in response.text
        assert "paged likes 001" not in response.text
        assert "第 1 / 2 页" in response.text
        assert "page_size=24" in response.text

        desktop_page = client.get("/likes?p=2&page_size=24")

        assert desktop_page.status_code == 200
        assert desktop_page.text.count('class="card"') == 2
        assert "paged likes 001" in desktop_page.text
        assert "paged likes 000" in desktop_page.text


def test_likes_home_renders_numbered_pagination_with_first_and_last_links() -> None:
    with isolated_web_db() as conn:
        for index in range(90):
            insert_item_for_paging(conn, "likes", index)
        client = TestClient(web_app.app)

        response = client.get("/likes?p=5&page_size=8")

        assert response.status_code == 200
        assert 'aria-label="分页"' in response.text
        assert 'href="/likes?p=1&amp;page_size=8">首页</a>' in response.text
        assert 'href="/likes?p=12&amp;page_size=8">末页</a>' in response.text
        assert 'aria-current="page">5</span>' in response.text
        assert 'href="/likes?p=4&amp;page_size=8">4</a>' in response.text
        assert 'href="/likes?p=7&amp;page_size=8">7</a>' in response.text
        assert 'pagination-ellipsis' in response.text
        assert "第 5 / 12 页" in response.text


def test_favorites_home_supports_mobile_load_more_and_desktop_pagination() -> None:
    with isolated_web_db() as conn:
        page_size = web_app.HOME_PAGE_SIZE
        for index in range(page_size + 1):
            insert_item_for_paging(conn, "favorites", index)
        client = TestClient(web_app.app)

        response = client.get("/")

        assert response.status_code == 200
        assert f"paged favorites {page_size:03d}" in response.text
        assert "paged favorites 000" not in response.text
        assert f"/page?offset={page_size}" in response.text
        assert "/?p=2" in response.text

        desktop_page = client.get("/?p=2")

        assert desktop_page.status_code == 200
        assert "paged favorites 000" in desktop_page.text
        assert "第 2 / 2 页" in desktop_page.text


def test_empty_favorites_home_redirects_to_scan_setup() -> None:
    with isolated_web_db():
        client = TestClient(web_app.app)

        response = client.get("/", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/setup"


def test_empty_likes_home_uses_user_facing_sync_state_instead_of_cli_hint() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "fav-1", "favorite only", "fav author")
        jobs.enqueue_job("sync_likes", user_id="default", payload={"content_kind": "likes"})
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert "正在整理喜欢" in response.text
        assert "后台同步完成后会自动出现在这里" in response.text
        assert "recall crawl-likes" not in response.text
        assert "recall crawl" not in response.text
        assert 'hx-get="/likes/empty-status"' in response.text
        assert "去维护中心" in response.text


def test_empty_likes_sync_state_shows_progress_eta_and_waiting_motion() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "fav-1", "favorite only", "fav author")
        job_id = jobs.enqueue_job("sync_likes", user_id="default", payload={"content_kind": "likes"})
        started_at = datetime.now(timezone.utc) - timedelta(seconds=75)
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (started_at, job_id),
        )
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert "正在读取抖音数据" in response.text
        assert "已等待" in response.text
        assert "预计还需" in response.text
        assert 'role="progressbar"' in response.text
        assert "empty-wait-dots" in response.text


def test_likes_home_shows_index_progress_without_hiding_local_items() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "fav-1", "favorite only", "fav author")
        insert_item(conn, "likes", "like-1", "liked first", "like author")
        insert_item(conn, "likes", "like-2", "liked second", "like author")
        conn.execute("INSERT INTO likes_vec (id) VALUES ('default:like-1')")
        job_id = jobs.enqueue_job("index", user_id="default", payload={"content_kind": "likes"})
        started_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (started_at, job_id),
        )
        client = TestClient(web_app.app)

        response = client.get("/likes")

        assert response.status_code == 200
        assert "liked first" in response.text
        assert "liked second" in response.text
        assert "正在建立喜欢搜索索引" in response.text
        assert "1 / 2 已索引" in response.text
        assert 'role="progressbar"' in response.text


def test_existing_items_use_stable_background_sync_banner_for_favorites_and_likes() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "fav-1", "favorite item", "fav author")
        insert_item(conn, "likes", "like-1", "liked item", "like author")
        favorite_job_id = jobs.enqueue_job("sync_favorites", user_id="default", payload={"content_kind": "favorites"})
        like_job_id = jobs.enqueue_job("sync_likes", user_id="default", payload={"content_kind": "likes"})
        started_at = datetime.now(timezone.utc) - timedelta(seconds=75)
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (started_at, like_job_id),
        )
        client = TestClient(web_app.app)

        favorites_response = client.get("/")
        likes_response = client.get("/likes")

        assert favorites_response.status_code == 200
        assert likes_response.status_code == 200
        assert "正在后台更新收藏" in favorites_response.text
        assert "正在后台更新喜欢" in likes_response.text
        assert "可以继续浏览" in favorites_response.text
        assert "可以继续浏览" in likes_response.text
        assert "正在整理收藏" not in favorites_response.text
        assert "正在整理喜欢" not in likes_response.text
        assert "work-progress-spinner" in favorites_response.text
        assert "work-progress-spinner" in likes_response.text
        assert "work-progress-fill" in favorites_response.text
        assert "work-progress-fill" in likes_response.text
        assert 'hx-trigger="every 3s"' not in favorites_response.text
        assert 'hx-trigger="every 3s"' not in likes_response.text


def test_likes_author_and_category_pages_use_likes_tables() -> None:
    with isolated_web_db() as conn:
        fav_cat = insert_category(conn, "categories", "favorite category")
        like_cat = insert_category(conn, "like_categories", "like category")
        insert_item(conn, "favorites", "fav-1", "favorite only", "fav author")
        insert_item(conn, "likes", "like-1", "liked only", "like author")
        conn.execute("UPDATE favorites SET category_id = ? WHERE id = 'fav-1'", (fav_cat,))
        conn.execute("UPDATE likes SET category_id = ? WHERE id = 'like-1'", (like_cat,))
        client = TestClient(web_app.app)

        authors = client.get("/likes/authors")
        categories = client.get("/likes/categories")

        assert authors.status_code == 200
        assert "@like author" in authors.text
        assert "@fav author" not in authors.text
        assert "/likes?author=like%20author" in authors.text
        assert categories.status_code == 200
        assert "like category" in categories.text
        assert "favorite category" not in categories.text
        assert f"/likes?category={like_cat}" in categories.text


def test_likes_note_and_open_tracking_write_likes_tables() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "likes", "like-1", "liked only", "like author")
        client = TestClient(web_app.app)

        note_response = client.patch("/likes/like-1/note", data={"note": "keep this like"})
        track_response = client.post("/likes/track/open/like-1")

        note = conn.execute("SELECT user_note FROM likes WHERE id = 'like-1'").fetchone()
        recall_count = conn.execute("SELECT COUNT(*) AS c FROM recall_log").fetchone()["c"]
        like_recall_count = conn.execute("SELECT COUNT(*) AS c FROM like_recall_log").fetchone()["c"]
        last_recalled = conn.execute("SELECT last_recalled_at FROM likes WHERE id = 'like-1'").fetchone()

        assert note_response.status_code == 200
        assert "keep this like" in note_response.text
        assert track_response.status_code == 200
        assert track_response.json() == {"ok": True}
        assert note["user_note"] == "keep this like"
        assert recall_count == 0
        assert like_recall_count == 1
        assert last_recalled["last_recalled_at"] is not None


def test_likes_unlike_updates_likes_only_and_writes_unlike_log() -> None:
    with isolated_web_db() as conn:
        insert_item(conn, "favorites", "like-1", "favorite same id", "fav author")
        insert_item(conn, "likes", "like-1", "liked only", "like author")

        client = TestClient(web_app.app)
        response = client.post("/likes/like-1/unlike")

        like_row = conn.execute(
            "SELECT is_removed FROM likes WHERE id = 'like-1'"
        ).fetchone()
        fav_row = conn.execute(
            "SELECT is_removed FROM favorites WHERE id = 'like-1'"
        ).fetchone()
        log_row = conn.execute(
            "SELECT like_id, status, channel, error_message FROM unlike_log"
        ).fetchone()
        job_row = conn.execute(
            "SELECT user_id, kind, payload_json, status FROM job_queue"
        ).fetchone()

        assert response.status_code == 200
        assert response.text == ""
        assert like_row["is_removed"] == 1
        assert fav_row["is_removed"] == 0
        assert log_row["like_id"] == "like-1"
        assert log_row["status"] == "pending"
        assert log_row["channel"] == "web"
        assert log_row["error_message"] is None
        assert job_row["user_id"] == "default"
        assert job_row["kind"] == "uncollect"
        assert '"content_kind": "likes"' in job_row["payload_json"]
        assert '"aweme_id": "like-1"' in job_row["payload_json"]
        assert job_row["status"] == "pending"


if __name__ == "__main__":
    tests = [
        test_likes_home_reads_likes_table_and_hides_uncollect_action,
        test_likes_home_uses_video_publish_time_when_like_time_missing,
        test_likes_home_supports_mobile_load_more_and_desktop_pagination,
        test_likes_home_preserves_custom_page_size_for_full_desktop_rows,
        test_likes_home_renders_numbered_pagination_with_first_and_last_links,
        test_favorites_home_supports_mobile_load_more_and_desktop_pagination,
        test_likes_author_and_category_pages_use_likes_tables,
        test_likes_note_and_open_tracking_write_likes_tables,
        test_likes_unlike_updates_likes_only_and_writes_unlike_log,
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

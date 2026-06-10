"""
Web multi-user isolation tests.

Run:
    python tests/test_web_multitenant.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from fastapi.testclient import TestClient

from src import accounts
from src import db
from src import jobs
from src.config import settings
from src.web import app as web_app


@contextmanager
def isolated_web_accounts_db():
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
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, embedding BLOB)")

    original_web_get_connection = web_app.get_connection
    original_accounts_get_connection = accounts.get_connection
    original_jobs_get_connection = jobs.get_connection
    original_auth_required = settings.web_auth_required

    web_app.get_connection = lambda: conn
    accounts.get_connection = lambda: conn
    jobs.get_connection = lambda: conn
    settings.web_auth_required = True
    try:
        yield conn
    finally:
        web_app.get_connection = original_web_get_connection
        accounts.get_connection = original_accounts_get_connection
        jobs.get_connection = original_jobs_get_connection
        settings.web_auth_required = original_auth_required
        conn.close()


def insert_user_item(conn: sqlite3.Connection, user_id: str, item_id: str, title: str) -> None:
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, '2026-05-26 00:00:00')
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, user_id),
    )
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, author, video_url, cover_url, user_note,
            raw_json, favorited_at, first_seen_at, last_seen_at,
            is_removed, discovery_index, video_created_at
        ) VALUES (?, ?, ?, 'author', ?, NULL, NULL, '{}', NULL,
                  '2026-05-26', '2026-05-26', 0, 1, '2026-05-01')
        """,
        (user_id, item_id, title, f"https://example.test/{item_id}"),
    )


def test_home_page_only_shows_items_for_session_user() -> None:
    with isolated_web_accounts_db() as conn:
        insert_user_item(conn, "alice", "a1", "alice private item")
        insert_user_item(conn, "bob", "b1", "bob private item")
        alice_token = accounts.create_session("alice")

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        response = client.get("/")

        assert response.status_code == 200
        assert "alice private item" in response.text
        assert "bob private item" not in response.text
        assert "1 条收藏" in response.text


def test_protected_page_redirects_without_session_when_auth_required() -> None:
    with isolated_web_accounts_db():
        client = TestClient(web_app.app)
        response = client.get("/", follow_redirects=False)

        assert response.status_code in {302, 303, 307}
        assert response.headers["location"].startswith("/login")


def test_uncollect_route_enqueues_background_job_for_session_user() -> None:
    with isolated_web_accounts_db() as conn:
        insert_user_item(conn, "alice", "a1", "alice private item")
        insert_user_item(conn, "bob", "a1", "bob private item")
        alice_token = accounts.create_session("alice")

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        response = client.post("/favorites/a1/uncollect")

        alice_row = conn.execute(
            "SELECT is_removed FROM favorites WHERE user_id = 'alice' AND id = 'a1'"
        ).fetchone()
        bob_row = conn.execute(
            "SELECT is_removed FROM favorites WHERE user_id = 'bob' AND id = 'a1'"
        ).fetchone()
        job = conn.execute(
            "SELECT user_id, kind, payload_json, status FROM job_queue"
        ).fetchone()
        log = conn.execute(
            "SELECT user_id, favorite_id, status, channel FROM uncollect_log"
        ).fetchone()

        assert response.status_code == 200
        assert response.text == ""
        assert alice_row["is_removed"] == 1
        assert bob_row["is_removed"] == 0
        assert job["user_id"] == "alice"
        assert job["kind"] == "uncollect"
        assert '"aweme_id": "a1"' in job["payload_json"]
        assert '"content_kind": "favorites"' in job["payload_json"]
        assert job["status"] == "pending"
        assert dict(log) == {
            "user_id": "alice",
            "favorite_id": "a1",
            "status": "pending",
            "channel": "web",
        }


def test_job_enqueue_routes_scope_sync_and_index_to_session_user() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        sync_response = client.post("/jobs/sync", data={"kind": "likes", "max_pages": "7"})
        index_response = client.post("/jobs/index", data={"kind": "favorites", "force": "true"})

        rows = conn.execute(
            "SELECT user_id, kind, payload_json, status FROM job_queue ORDER BY id"
        ).fetchall()
        assert sync_response.status_code == 200
        assert index_response.status_code == 200
        assert [(r["user_id"], r["kind"], r["status"]) for r in rows] == [
            ("alice", "sync_likes", "pending"),
            ("alice", "index", "pending"),
        ]
        assert '"max_pages": 7' in rows[0]["payload_json"]
        assert '"content_kind": "likes"' in rows[0]["payload_json"]
        assert '"content_kind": "favorites"' in rows[1]["payload_json"]
        assert '"force": true' in rows[1]["payload_json"]


def test_jobs_status_page_only_shows_current_session_user_jobs() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('bob', 'Bob', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        jobs.enqueue_job("sync_favorites", user_id="alice", payload={"content_kind": "favorites"})
        jobs.enqueue_job("sync_likes", user_id="bob", payload={"content_kind": "likes"})

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        response = client.get("/jobs")

        assert response.status_code == 200
        assert "后台队列" in response.text
        assert "sync_favorites" in response.text
        assert "sync_likes" not in response.text


def test_auth_profile_refresh_updates_current_session_user_only() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('bob', 'Bob', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        original_fetch = getattr(web_app, "_fetch_douyin_profile_for_user", None)
        web_app._fetch_douyin_profile_for_user = lambda user_id: {
            "nickname": f"{user_id}抖音",
            "unique_id": "alice_dy",
            "sec_uid": "SEC_ALICE",
            "avatar_url": "https://example.com/alice.jpeg",
        }

        try:
            client = TestClient(web_app.app)
            client.cookies.set(settings.session_cookie_name, alice_token)
            response = client.post("/auth/profile/refresh")
        finally:
            if original_fetch is None:
                delattr(web_app, "_fetch_douyin_profile_for_user")
            else:
                web_app._fetch_douyin_profile_for_user = original_fetch

        alice = conn.execute("SELECT * FROM users WHERE id = 'alice'").fetchone()
        bob = conn.execute("SELECT * FROM users WHERE id = 'bob'").fetchone()
        assert response.status_code == 200
        assert "alice抖音" in response.text
        assert alice["douyin_nickname"] == "alice抖音"
        assert alice["douyin_unique_id"] == "alice_dy"
        assert bob["douyin_nickname"] is None


def test_auth_profile_refresh_reports_expired_login_without_overwriting_existing_profile() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_avatar_url, created_at
            ) VALUES (
                'alice', 'Alice', '旧昵称', 'https://example.com/old.jpeg', '2026-05-26 00:00:00'
            )
            """
        )
        alice_token = accounts.create_session("alice")
        original_fetch = getattr(web_app, "_fetch_douyin_profile_for_user", None)

        def expired(_user_id: str) -> dict:
            raise RuntimeError("抖音登录态失效，请先运行 `recall auth` 扫码授权。API 返回：用户未登录")

        web_app._fetch_douyin_profile_for_user = expired
        try:
            client = TestClient(web_app.app)
            client.cookies.set(settings.session_cookie_name, alice_token)
            response = client.post("/auth/profile/refresh")
        finally:
            if original_fetch is None:
                delattr(web_app, "_fetch_douyin_profile_for_user")
            else:
                web_app._fetch_douyin_profile_for_user = original_fetch

        row = conn.execute("SELECT douyin_nickname, douyin_avatar_url FROM users WHERE id = 'alice'").fetchone()
        assert response.status_code == 200
        assert "登录态已过期，请重新绑定" in response.text
        assert row["douyin_nickname"] == "旧昵称"
        assert row["douyin_avatar_url"] == "https://example.com/old.jpeg"


if __name__ == "__main__":
    tests = [
        test_home_page_only_shows_items_for_session_user,
        test_protected_page_redirects_without_session_when_auth_required,
        test_uncollect_route_enqueues_background_job_for_session_user,
        test_job_enqueue_routes_scope_sync_and_index_to_session_user,
        test_jobs_status_page_only_shows_current_session_user_jobs,
        test_auth_profile_refresh_updates_current_session_user_only,
        test_auth_profile_refresh_reports_expired_login_without_overwriting_existing_profile,
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

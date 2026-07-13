"""
Web multi-user isolation tests.

Run:
    python tests/test_web_multitenant.py
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from src import accounts
from src import db
from src import jobs
from src import onboarding
from src.categorize import cluster
from src.config import settings
from src.web import app as web_app
from src.web import douyin_auth
from src.web import security as web_security
from src.web import job_service
from src.web import helpers as web_helpers
from src.web import middleware as web_middleware
from src.web.routes import content as content_routes
from src.web.routes import maintenance as maintenance_routes


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
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, user_id TEXT, embedding BLOB)")

    original_web_get_connection = web_helpers._db_get_connection
    original_accounts_get_connection = accounts.get_connection
    original_cluster_get_connection = cluster.get_connection
    original_jobs_get_connection = jobs.get_connection
    original_onboarding_get_connection = onboarding.get_connection
    original_maintenance_get_connection = maintenance_routes.maintenance.get_connection
    original_security_get_connection = web_security.get_connection
    original_auth_required = settings.web_auth_required

    web_helpers._db_get_connection = lambda: conn
    accounts.get_connection = lambda: conn
    cluster.get_connection = lambda: conn
    jobs.get_connection = lambda: conn
    onboarding.get_connection = lambda: conn
    maintenance_routes.maintenance.get_connection = lambda: conn
    web_security.get_connection = lambda: conn
    settings.web_auth_required = True
    try:
        yield conn
    finally:
        web_helpers._db_get_connection = original_web_get_connection
        accounts.get_connection = original_accounts_get_connection
        cluster.get_connection = original_cluster_get_connection
        jobs.get_connection = original_jobs_get_connection
        onboarding.get_connection = original_onboarding_get_connection
        maintenance_routes.maintenance.get_connection = original_maintenance_get_connection
        web_security.get_connection = original_security_get_connection
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
        conn.execute(
            "UPDATE users SET douyin_nickname = 'Alice 抖音账号' WHERE id = 'alice'"
        )
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


def test_current_user_database_resolution_runs_off_the_event_loop() -> None:
    original_user_from_session = accounts.user_from_session
    original_ensure_default_user = accounts.ensure_default_user
    original_auth_required = settings.web_auth_required
    worker_thread_ids: list[int] = []

    def fake_user_from_session(_token):
        worker_thread_ids.append(threading.get_ident())
        return None

    def fake_ensure_default_user():
        worker_thread_ids.append(threading.get_ident())
        return {"id": "default", "display_name": "Default"}

    async def exercise() -> tuple[Response, int]:
        event_loop_thread_id = threading.get_ident()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "root_path": "",
            }
        )

        async def call_next(_request: Request) -> Response:
            return Response("ok")

        response = await web_middleware.attach_current_user(request, call_next)
        assert request.state.user_id == "default"
        return response, event_loop_thread_id

    accounts.user_from_session = fake_user_from_session
    accounts.ensure_default_user = fake_ensure_default_user
    settings.web_auth_required = False
    try:
        response, event_loop_thread_id = asyncio.run(exercise())
    finally:
        accounts.user_from_session = original_user_from_session
        accounts.ensure_default_user = original_ensure_default_user
        settings.web_auth_required = original_auth_required

    assert response.status_code == 200
    assert len(worker_thread_ids) == 2
    assert all(thread_id != event_loop_thread_id for thread_id in worker_thread_ids)


def test_login_rate_limit_blocks_repeated_failures_and_sets_retry_after() -> None:
    original_max_attempts = settings.login_rate_limit_max_attempts
    original_window = settings.login_rate_limit_window_seconds
    with isolated_web_accounts_db() as conn:
        settings.login_rate_limit_max_attempts = 2
        settings.login_rate_limit_window_seconds = 60
        client = TestClient(web_app.app)
        try:
            first = client.post(
                "/login",
                data={"invite_code": "wrong", "display_name": "Alice", "next": "/"},
                follow_redirects=False,
            )
            second = client.post(
                "/login",
                data={"invite_code": "wrong", "display_name": "Alice", "next": "/"},
                follow_redirects=False,
            )
            third = client.post(
                "/login",
                data={"invite_code": "wrong", "display_name": "Alice", "next": "/"},
                follow_redirects=False,
            )
        finally:
            settings.login_rate_limit_max_attempts = original_max_attempts
            settings.login_rate_limit_window_seconds = original_window

        assert first.status_code == 400
        assert second.status_code == 429
        assert third.status_code == 429
        assert int(second.headers["retry-after"]) > 0
        assert "尝试次数过多" in third.text
        counts = conn.execute(
            "SELECT scope, failed_count FROM login_rate_limits ORDER BY scope"
        ).fetchall()
        assert [(row["scope"], row["failed_count"]) for row in counts] == [
            ("ip", 2),
            ("ip_invite", 2),
        ]


def test_login_redirect_target_only_allows_local_absolute_paths() -> None:
    rejected = (
        "https://evil.example/path",
        "//evil.example/path",
        "///evil.example/path",
        r"\evil.example\path",
        r"/\evil.example/path",
        "/%5Cevil.example/path",
        "/%255Cevil.example/path",
        "%2F%2Fevil.example/path",
        "/%2Fevil.example/path",
        "/%252Fevil.example/path",
        "/\r/evil.example",
        "/" + "a" * 4096,
    )
    allowed = (
        "/",
        "/memories",
        "/memories?source=likes&sort=recent",
        "/search?q=//evil.example",
        "/search?q=https%3A%2F%2Fevil.example%2Fa",
        "/search?q=a%26b",
    )

    for value in rejected:
        assert web_security.safe_local_redirect_target(value) == "/"
    for value in allowed:
        assert web_security.safe_local_redirect_target(value) == value


def test_login_form_and_success_redirect_sanitize_next_target() -> None:
    with isolated_web_accounts_db():
        accounts.ensure_default_user()
        malicious_code = accounts.create_invite(
            created_by_user_id="default",
            code="REDIRECT-BLOCK-CODE",
        )
        local_code = accounts.create_invite(
            created_by_user_id="default",
            code="REDIRECT-LOCAL-CODE",
        )
        client = TestClient(web_app.app)

        form_response = client.get(
            "/login",
            params={"next": "https://evil.example/steal"},
        )
        blocked_response = client.post(
            "/login",
            data={
                "invite_code": malicious_code,
                "display_name": "Alice",
                "next": "//evil.example/steal",
            },
            follow_redirects=False,
        )
        local_response = client.post(
            "/login",
            data={
                "invite_code": local_code,
                "display_name": "Bob",
                "next": "/memories?source=likes&sort=recent",
            },
            follow_redirects=False,
        )

        assert form_response.status_code == 200
        assert 'name="next" value="/"' in form_response.text
        assert "evil.example" not in form_response.text
        assert blocked_response.status_code == 303
        assert blocked_response.headers["location"] == "/"
        assert local_response.status_code == 303
        assert local_response.headers["location"] == "/memories?source=likes&sort=recent"


def test_secure_session_cookie_is_used_for_login_and_logout() -> None:
    original_secure = settings.session_cookie_secure
    with isolated_web_accounts_db():
        accounts.ensure_default_user()
        code = accounts.create_invite(created_by_user_id="default", code="COOKIE-CODE")
        settings.session_cookie_secure = True
        client = TestClient(web_app.app, base_url="https://testserver")
        try:
            login_response = client.post(
                "/login",
                data={"invite_code": code, "display_name": "Alice", "next": "/"},
                follow_redirects=False,
            )
            logout_response = client.get("/logout", follow_redirects=False)
        finally:
            settings.session_cookie_secure = original_secure

        login_cookie = login_response.headers["set-cookie"].lower()
        logout_cookie = logout_response.headers["set-cookie"].lower()
        assert login_response.status_code == 303
        assert "secure" in login_cookie
        assert "httponly" in login_cookie
        assert "samesite=lax" in login_cookie
        assert "path=/" in login_cookie
        assert "secure" in logout_cookie
        assert "httponly" in logout_cookie


def test_public_authenticated_bind_rejects_insecure_session_cookie() -> None:
    original_host = settings.web_host
    original_auth_required = settings.web_auth_required
    original_secure = settings.session_cookie_secure
    settings.web_host = "0.0.0.0"
    settings.web_auth_required = True
    settings.session_cookie_secure = False
    try:
        try:
            web_security.validate_web_security_config()
        except RuntimeError as exc:
            assert "SESSION_COOKIE_SECURE=true" in str(exc)
        else:
            raise AssertionError("public authenticated bind should require secure cookies")
        settings.session_cookie_secure = True
        web_security.validate_web_security_config()
    finally:
        settings.web_host = original_host
        settings.web_auth_required = original_auth_required
        settings.session_cookie_secure = original_secure


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


def test_hx_sync_redirects_to_the_kind_specific_empty_status() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) "
            "VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)

        response = client.post(
            "/jobs/sync",
            data={"kind": "likes", "max_pages": "7"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/likes/empty-status"
        row = conn.execute(
            "SELECT user_id, kind, payload_json FROM job_queue"
        ).fetchone()
        assert row["user_id"] == "alice"
        assert row["kind"] == "sync_likes"
        assert '"content_kind": "likes"' in row["payload_json"]


def test_first_run_jobs_enqueue_sync_and_index_once_per_session_user() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )

        first = job_service.enqueue_first_run_jobs("alice")
        second = job_service.enqueue_first_run_jobs("alice")

        rows = conn.execute(
            "SELECT user_id, kind, payload_json, status FROM job_queue ORDER BY id"
        ).fetchall()
        assert first == ["sync_favorites", "sync_likes", "index", "index"]
        assert second == []
        assert [(r["user_id"], r["kind"], r["status"]) for r in rows] == [
            ("alice", "sync_favorites", "pending"),
            ("alice", "sync_likes", "pending"),
            ("alice", "index", "pending"),
            ("alice", "index", "pending"),
        ]
        assert '"content_kind": "favorites"' in rows[0]["payload_json"]
        assert '"content_kind": "likes"' in rows[1]["payload_json"]
        assert '"content_kind": "favorites"' in rows[2]["payload_json"]
        assert '"content_kind": "likes"' in rows[3]["payload_json"]


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


def test_jobs_page_hides_internal_error_details_from_failed_jobs() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice", payload={"content_kind": "favorites"})
        raw_error = (
            "Traceback (most recent call last):\n"
            r"  File D:\douyinclaude\src\jobs.py, line 456, in run_job" "\n"
            r"RuntimeError: command: uv run python -m src.cli crawl --debug-xhr D:\Users\Alice\data\recall.db"
        )
        conn.execute(
            "UPDATE job_queue SET status = 'failed', error_message = ? WHERE id = ?",
            (raw_error, job_id),
        )

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        page = client.get("/jobs")
        fragment = client.get("/jobs/status")

        for response in (page, fragment):
            assert response.status_code == 200
            assert "任务失败，请打开诊断包或日志查看详情。" in response.text
            assert "Traceback" not in response.text
            assert "D:\\" not in response.text
            assert "uv run" not in response.text
            assert "jobs.py" not in response.text
            assert "recall.db" not in response.text

        stored = conn.execute("SELECT error_message FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        assert stored["error_message"] == raw_error


def test_jobs_status_recovers_stale_running_job_before_rendering() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice", payload={"content_kind": "favorites"})
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'running', started_at = ?, attempts = 1
            WHERE id = ?
            """,
            (datetime.now(timezone.utc) - timedelta(hours=2), job_id),
        )

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        response = client.get("/jobs/status")

        row = conn.execute(
            "SELECT status, started_at, next_run_at, error_message FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert response.status_code == 200
        assert "running" not in response.text
        assert "pending" in response.text
        assert row["status"] == "pending"
        assert row["started_at"] is None
        assert "stale running job recovered" in row["error_message"]


def test_auth_profile_refresh_updates_current_session_user_only() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
        )
        conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES ('bob', 'Bob', '2026-05-26 00:00:00')"
        )
        alice_token = accounts.create_session("alice")
        original_fetch = douyin_auth.fetch_douyin_profile_for_user
        douyin_auth.fetch_douyin_profile_for_user = lambda user_id: {
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
            douyin_auth.fetch_douyin_profile_for_user = original_fetch

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
        original_fetch = douyin_auth.fetch_douyin_profile_for_user

        def expired(_user_id: str) -> dict:
            raise RuntimeError("抖音登录态失效，请先运行 `recall auth` 扫码授权。API 返回：用户未登录")

        douyin_auth.fetch_douyin_profile_for_user = expired
        try:
            client = TestClient(web_app.app)
            client.cookies.set(settings.session_cookie_name, alice_token)
            response = client.post("/auth/profile/refresh")
        finally:
            douyin_auth.fetch_douyin_profile_for_user = original_fetch

        row = conn.execute("SELECT douyin_nickname, douyin_avatar_url FROM users WHERE id = 'alice'").fetchone()
        assert response.status_code == 200
        assert "登录态已过期，请重新绑定" in response.text
        assert row["douyin_nickname"] == "旧昵称"
        assert row["douyin_avatar_url"] == "https://example.com/old.jpeg"


def test_auth_logout_clears_current_douyin_profile_only_and_preserves_items() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id,
                douyin_sec_uid, douyin_avatar_url, douyin_profile_updated_at, created_at
            ) VALUES (
                'alice', 'Alice', 'Alice抖音', 'alice_dy',
                'SEC_ALICE', 'https://example.com/alice.jpeg', '2026-07-05 10:00:00',
                '2026-05-26 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id,
                douyin_sec_uid, douyin_avatar_url, douyin_profile_updated_at, created_at
            ) VALUES (
                'bob', 'Bob', 'Bob抖音', 'bob_dy',
                'SEC_BOB', 'https://example.com/bob.jpeg', '2026-07-05 10:00:00',
                '2026-05-26 00:00:00'
            )
            """
        )
        insert_user_item(conn, "alice", "fav-alice", "Alice 收藏")
        insert_user_item(conn, "bob", "fav-bob", "Bob 收藏")
        alice_token = accounts.create_session("alice")
        cleanup_calls: list[str] = []
        original_start_cleanup = douyin_auth.start_douyin_logout_cleanup
        douyin_auth.start_douyin_logout_cleanup = cleanup_calls.append

        try:
            client = TestClient(web_app.app)
            client.cookies.set(settings.session_cookie_name, alice_token)
            response = client.post("/auth/logout", follow_redirects=False)
        finally:
            douyin_auth.start_douyin_logout_cleanup = original_start_cleanup

        alice = conn.execute("SELECT * FROM users WHERE id = 'alice'").fetchone()
        bob = conn.execute("SELECT * FROM users WHERE id = 'bob'").fetchone()
        item_count = conn.execute("SELECT COUNT(*) AS c FROM favorites").fetchone()["c"]
        assert response.status_code == 303
        assert response.headers["location"] == "/auth"
        assert cleanup_calls == ["alice"]
        assert alice["douyin_nickname"] is None
        assert alice["douyin_unique_id"] is None
        assert alice["douyin_sec_uid"] is None
        assert alice["douyin_avatar_url"] is None
        assert alice["douyin_profile_updated_at"] is None
        assert bob["douyin_nickname"] == "Bob抖音"
        assert item_count == 2


def test_auth_add_account_creates_session_user_and_starts_qr_scan() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id, created_at
            ) VALUES (
                'default', '本地默认用户', '主账号', 'main_dy', '2026-05-26 00:00:00'
            )
            """
        )
        token = accounts.create_session("default")
        calls: list[tuple[str, bool]] = []
        original_start = douyin_auth.ensure_douyin_auth_started

        def fake_start(user_id: str, *, force: bool = False) -> None:
            calls.append((user_id, force))

        douyin_auth.ensure_douyin_auth_started = fake_start
        try:
            client = TestClient(web_app.app)
            client.cookies.set(settings.session_cookie_name, token)
            response = client.post("/auth/add", follow_redirects=False)
        finally:
            douyin_auth.ensure_douyin_auth_started = original_start

        users = conn.execute("SELECT id, display_name FROM users ORDER BY created_at, id").fetchall()
        new_token = client.cookies.get(settings.session_cookie_name, domain="testserver.local")
        switched_user = accounts.user_from_session(new_token)

        assert response.status_code == 303
        assert response.headers["location"] == "/auth"
        assert len(users) == 2
        assert users[1]["id"] != "default"
        assert users[1]["display_name"].startswith("抖音账号")
        assert switched_user["id"] == users[1]["id"]
        assert calls == [(users[1]["id"], True)]


def test_auth_switch_account_sets_session_cookie_for_selected_bound_user() -> None:
    with isolated_web_accounts_db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id, created_at
            ) VALUES (
                'alice', 'Alice', 'Alice抖音', 'alice_dy', '2026-05-26 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id, created_at
            ) VALUES (
                'bob', 'Bob', 'Bob抖音', 'bob_dy', '2026-05-27 00:00:00'
            )
            """
        )
        alice_token = accounts.create_session("alice")

        client = TestClient(web_app.app)
        client.cookies.set(settings.session_cookie_name, alice_token)
        response = client.post("/auth/switch", data={"user_id": "bob"}, follow_redirects=False)
        switched_user = accounts.user_from_session(
            client.cookies.get(settings.session_cookie_name, domain="testserver.local")
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/auth"
        assert switched_user["id"] == "bob"


def _authenticated_client_for_user(conn: sqlite3.Connection, user_id: str = "alice") -> TestClient:
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, '2026-05-26 00:00:00')
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, user_id),
    )
    token = accounts.create_session(user_id)
    client = TestClient(web_app.app)
    client.cookies.set(settings.session_cookie_name, token)
    return client


def _set_douyin_auth_session(
    user_id: str,
    *,
    status: str,
    message: str = "",
    qr_path: str | None = None,
) -> None:
    douyin_auth.set_auth_session(
        user_id,
        {
            "status": status,
            "message": message,
            "qr_path": qr_path,
        },
    )


def test_auth_status_fragment_renders_qr_scan_and_confirmed_states_without_phone_scan() -> None:
    with isolated_web_accounts_db() as conn:
        client = _authenticated_client_for_user(conn, "alice")
        try:
            _set_douyin_auth_session(
                "alice",
                status="qr_ready",
                message="请用抖音 App 扫描二维码",
                qr_path=r"D:\runtime\alice\auth\douyin-login.png",
            )
            qr_response = client.get("/auth/status")
            _set_douyin_auth_session("alice", status="scan_pending", message="")
            scan_response = client.get("/auth/status")
            _set_douyin_auth_session("alice", status="confirmed", message="")
            confirmed_response = client.get("/auth/status")
        finally:
            douyin_auth.clear_auth_session("alice")

        assert qr_response.status_code == 200
        assert "打开抖音 App 扫一扫" in qr_response.text
        assert "/auth/qr-image" in qr_response.text
        assert "手机已扫码，等待你在抖音 App 确认登录" in scan_response.text
        assert "确认完成，正在保存账号" in confirmed_response.text


def test_setup_auth_fragments_render_state_transitions_without_phone_scan() -> None:
    with isolated_web_accounts_db() as conn:
        client = _authenticated_client_for_user(conn, "alice")
        qr_path = r"D:\runtime\alice\auth\douyin-login.png"
        qr_version = hashlib.sha256(qr_path.encode("utf-8")).hexdigest()[:16]
        try:
            _set_douyin_auth_session(
                "alice",
                status="qr_ready",
                message="请用抖音 App 扫描二维码",
                qr_path=qr_path,
            )
            qr_response = client.get("/setup/auth-status")
            unchanged_qr_response = client.get(f"/setup/scan-state?qr={qr_version}")
            refreshed_qr_response = client.get("/setup/scan-state?qr=wrong-version")
            _set_douyin_auth_session("alice", status="scan_pending", message="")
            unchanged_scan_response = client.get("/setup/scan-state?state=scan_pending")
            scan_response = client.get("/setup/scan-state?state=qr_ready")
            _set_douyin_auth_session("alice", status="confirmed", message="")
            unchanged_confirmed_response = client.get("/setup/scan-state?state=confirmed")
            confirmed_response = client.get("/setup/scan-state?state=scan_pending")
        finally:
            douyin_auth.clear_auth_session("alice")

        assert qr_response.status_code == 200
        assert "打开抖音 App 扫一扫" in qr_response.text
        assert 'data-setup-auth-state="qr_ready"' in qr_response.text
        assert unchanged_qr_response.status_code == 204
        assert refreshed_qr_response.status_code == 200
        assert "打开抖音 App 扫一扫" in refreshed_qr_response.text
        assert unchanged_scan_response.status_code == 204
        assert "手机已扫码，等待你在抖音 App 确认登录" in scan_response.text
        assert unchanged_confirmed_response.status_code == 204
        assert "扫码成功" in confirmed_response.text
        assert "正在登录并准备同步" in confirmed_response.text


def test_auth_and_setup_failed_fragments_hide_sensitive_error_details() -> None:
    sensitive_message = (
        "Traceback (most recent call last): "
        r"command: uv run python -m src.cli auth D:\douyinclaude\data\users\alice\auth\douyin-login.png"
    )
    with isolated_web_accounts_db() as conn:
        client = _authenticated_client_for_user(conn, "alice")
        try:
            _set_douyin_auth_session("alice", status="failed", message=sensitive_message)
            auth_response = client.get("/auth/status")
            setup_response = client.get("/setup/auth-status")
        finally:
            douyin_auth.clear_auth_session("alice")

        assert auth_response.status_code == 200
        assert setup_response.status_code == 200
        combined = auth_response.text + setup_response.text
        assert "授权过程出错，请重新生成二维码后再试。" in combined
        assert "Traceback" not in combined
        assert "D:\\" not in combined
        assert "uv run" not in combined
        assert "command:" not in combined


def test_public_douyin_auth_message_hides_internal_screenshot_path() -> None:
    message = r"等待扫码超时（180s）。二维码截图：D:\测试\DouyinRecall\data\users\abc\auth\douyin-login.png"

    cleaned = douyin_auth.public_douyin_auth_message(message)

    assert cleaned == "扫码超时，请重新生成二维码后再试。"
    assert "D:\\" not in cleaned
    assert "douyin-login.png" not in cleaned
    assert "二维码截图" not in cleaned


def test_public_operation_error_message_hides_paths_tracebacks_and_commands() -> None:
    message = (
        "Traceback (most recent call last):\n"
        r"  File D:\douyinclaude\src\web\app.py, line 1434, in restore_backup" "\n"
        r"sqlite3.OperationalError: unable to open database file D:\Users\me\data\recall.db" "\n"
        r"command: uv run python -m src.cli diagnose"
    )

    cleaned = maintenance_routes.public_operation_error_message("恢复失败", message)

    assert cleaned == "恢复失败，请打开诊断包或日志查看详情。"
    assert "Traceback" not in cleaned
    assert "D:\\" not in cleaned
    assert "uv run" not in cleaned
    assert "app.py" not in cleaned


def test_public_maintenance_status_summary_hides_internal_details() -> None:
    original_status = maintenance_routes.maintenance.get_maintenance_status
    raw_path = r"D:\douyinclaude\data\exports\recall-backup-secret.db"
    try:
        maintenance_routes.maintenance.get_maintenance_status = lambda user_id, include_update=False: {
            "backups": {
                "output_dir": r"D:\douyinclaude\data\exports",
                "latest": {
                    "name": "recall-backup-secret.db",
                    "path": raw_path,
                    "size_bytes": 123,
                    "modified_at": "2026-07-07T00:00:00+00:00",
                },
                "items": [
                    {
                        "name": "recall-backup-secret.db",
                        "path": raw_path,
                        "size_bytes": 123,
                        "modified_at": "2026-07-07T00:00:00+00:00",
                    }
                ],
                "retention": {
                    "keep_latest": 8,
                    "kept": [{"name": "recall-backup-secret.db", "path": raw_path}],
                    "delete_candidates": [],
                    "protected": [],
                },
            },
            "auth": {
                "needs_rebind": True,
                "latest_error": {
                    "message": (
                        "Traceback (most recent call last): "
                        r"command: uv run python -m src.cli crawl D:\douyinclaude\data\users\alice"
                    )
                },
                "errors": [
                    {
                        "message": (
                            "Traceback (most recent call last): "
                            r"command: uv run python -m src.cli crawl D:\douyinclaude\data\users\alice"
                        )
                    }
                ],
            },
            "update": {
                "error": (
                    "Traceback (most recent call last): "
                    r"command: uv run python -m src.cli update D:\douyinclaude"
                )
            },
            "sections": {
                "failed_tasks": {
                    "status": "failed",
                    "ok": False,
                    "message": (
                        "Traceback (most recent call last): "
                        r"command: uv run python -m src.cli jobs D:\douyinclaude\data\recall.db"
                    ),
                    "details": {
                        "count": 1,
                        "items": [
                            {
                                "id": 1,
                                "kind": "sync_favorites",
                                "error_message": (
                                    "Traceback (most recent call last): "
                                    r"command: uv run python -m src.cli crawl D:\douyinclaude\data\recall.db"
                                ),
                                "payload": {
                                    "diagnostic_path": r"D:\douyinclaude\data\diagnostics\bundle.zip"
                                },
                            }
                        ],
                        "retrying_items": [
                            {
                                "id": 2,
                                "kind": "sync_likes",
                                "error_message": (
                                    "Traceback (most recent call last): "
                                    r"command: uv run python -m src.cli crawl-likes D:\douyinclaude"
                                ),
                            }
                        ],
                    },
                },
                "backup": {
                    "status": "ok",
                    "ok": True,
                    "message": "备份正常",
                    "details": {
                        "output_dir": r"D:\douyinclaude\data\exports",
                        "latest": {"path": raw_path, "name": "recall-backup-secret.db"},
                    },
                },
            },
        }

        public = maintenance_routes.public_maintenance_status_for_template("alice")
    finally:
        maintenance_routes.maintenance.get_maintenance_status = original_status

    text = json.dumps(public, ensure_ascii=False)
    assert "本机备份目录" in text
    assert public["backups"]["items"][0]["path"] == "recall-backup-secret.db"
    assert public["backups"]["latest"]["path"] == "recall-backup-secret.db"
    assert "Traceback" not in text
    assert "D:\\" not in text
    assert "uv run" not in text
    assert "command:" not in text


if __name__ == "__main__":
    tests = [
        test_home_page_only_shows_items_for_session_user,
        test_protected_page_redirects_without_session_when_auth_required,
        test_login_redirect_target_only_allows_local_absolute_paths,
        test_login_form_and_success_redirect_sanitize_next_target,
        test_uncollect_route_enqueues_background_job_for_session_user,
        test_job_enqueue_routes_scope_sync_and_index_to_session_user,
        test_hx_sync_redirects_to_the_kind_specific_empty_status,
        test_first_run_jobs_enqueue_sync_and_index_once_per_session_user,
        test_jobs_status_page_only_shows_current_session_user_jobs,
        test_jobs_page_hides_internal_error_details_from_failed_jobs,
        test_jobs_status_recovers_stale_running_job_before_rendering,
        test_auth_profile_refresh_updates_current_session_user_only,
        test_auth_profile_refresh_reports_expired_login_without_overwriting_existing_profile,
        test_auth_logout_clears_current_douyin_profile_only_and_preserves_items,
        test_auth_add_account_creates_session_user_and_starts_qr_scan,
        test_auth_switch_account_sets_session_cookie_for_selected_bound_user,
        test_auth_status_fragment_renders_qr_scan_and_confirmed_states_without_phone_scan,
        test_setup_auth_fragments_render_state_transitions_without_phone_scan,
        test_auth_and_setup_failed_fragments_hide_sensitive_error_details,
        test_public_douyin_auth_message_hides_internal_screenshot_path,
        test_public_operation_error_message_hides_paths_tracebacks_and_commands,
        test_public_maintenance_status_summary_hides_internal_details,
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

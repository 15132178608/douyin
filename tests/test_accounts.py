"""
Private-cloud account and invite tests.

Run:
    python tests/test_accounts.py
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from src import db
from src import accounts
from src import tenancy
from src.config import settings


@contextmanager
def isolated_accounts_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)

    original_get_connection = accounts.get_connection
    accounts.get_connection = lambda: conn
    try:
        yield conn
    finally:
        accounts.get_connection = original_get_connection
        conn.close()


def test_invite_claim_creates_user_and_session_without_storing_plain_code() -> None:
    with isolated_accounts_db() as conn:
        accounts.ensure_default_user()
        code = accounts.create_invite(created_by_user_id="default", code="FRIEND-CODE")

        user, token = accounts.claim_invite(code, display_name="Alice")

        assert user["id"] != "default"
        assert user["display_name"] == "Alice"
        assert token
        assert accounts.user_from_session(token)["id"] == user["id"]
        assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM invite_codes WHERE code_hash = ?",
            ("FRIEND-CODE",),
        ).fetchone()["c"] == 0
        invite = conn.execute("SELECT used_count, claimed_by_user_id FROM invite_codes").fetchone()
        assert invite["used_count"] == 1
        assert invite["claimed_by_user_id"] == user["id"]


def test_invite_code_cannot_be_reused_after_single_claim() -> None:
    with isolated_accounts_db():
        accounts.ensure_default_user()
        code = accounts.create_invite(created_by_user_id="default", code="ONE-TIME")
        accounts.claim_invite(code, display_name="Alice")

        try:
            accounts.claim_invite(code, display_name="Bob")
        except accounts.InviteError as e:
            assert "已被使用" in str(e)
        else:
            raise AssertionError("reused invite should fail")


def test_invite_claim_rolls_back_user_and_usage_when_session_creation_fails() -> None:
    with isolated_accounts_db() as conn:
        accounts.ensure_default_user()
        code = accounts.create_invite(created_by_user_id="default", code="ROLLBACK-CODE")
        original_create_session = accounts.create_session
        accounts.create_session = lambda _user_id, days=None: (_ for _ in ()).throw(
            RuntimeError("session insert failed")
        )
        try:
            try:
                accounts.claim_invite(code, display_name="Alice")
            except RuntimeError as exc:
                assert "session insert failed" in str(exc)
            else:
                raise AssertionError("claim should fail when session creation fails")
        finally:
            accounts.create_session = original_create_session

        assert conn.execute("SELECT used_count FROM invite_codes").fetchone()["used_count"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM web_sessions").fetchone()["c"] == 0


def test_single_use_invite_is_atomic_across_concurrent_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts-concurrency.db"
    original_path = settings.db_path
    db.close_connection()
    settings.db_path = db_path
    try:
        db.init_schema()
        accounts.ensure_default_user()
        code = accounts.create_invite(created_by_user_id="default", code="ATOMIC-CODE")
        barrier = threading.Barrier(2)

        def claim(name: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                user, _token = accounts.claim_invite(code, display_name=name)
                return "success", user["id"]
            except accounts.InviteError as exc:
                return "error", str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("Alice", "Bob")))

        assert [status for status, _ in results].count("success") == 1
        assert [status for status, _ in results].count("error") == 1
        conn = db.get_connection()
        assert conn.execute("SELECT used_count FROM invite_codes").fetchone()["used_count"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) AS c FROM web_sessions").fetchone()["c"] == 1
    finally:
        db.close_connection()
        settings.db_path = original_path


def test_user_profile_paths_are_isolated() -> None:
    alice = accounts.profile_path_for_user("alice")
    bob = accounts.profile_path_for_user("bob")

    assert alice != bob
    assert alice.name == "playwright_profile"
    assert bob.name == "playwright_profile"


def test_user_profile_paths_honor_configured_root(tmp_path: Path, monkeypatch) -> None:
    custom_root = tmp_path / "custom-users"
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", tmp_path / "empty-legacy-project", raising=False)

    assert accounts.profile_path_for_user("alice") == custom_root / "~u-alice" / "playwright_profile"
    for user_id in (None, "", "   "):
        assert (
            accounts.profile_path_for_user(user_id)
            == custom_root / "~u-default" / "playwright_profile"
        )


def test_untrusted_user_ids_stay_in_distinct_profile_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", tmp_path / "empty-legacy-project", raising=False)
    user_ids = (
        ".",
        "..",
        "../escape",
        r"..\escape",
        "/absolute",
        r"C:\escape",
        "alice/bob",
        r"alice\bob",
        "Alice",
        "alice",
        "alice.",
        "CON",
        "a" * 300,
    )

    paths = {
        user_id: accounts.profile_path_for_user(user_id)
        for user_id in user_ids
    }

    root = custom_root.resolve()
    for path in paths.values():
        relative = path.resolve().relative_to(root)
        assert len(relative.parts) == 2
        assert relative.parts[-1] == "playwright_profile"
    assert len(set(paths.values())) == len(user_ids)
    assert paths["alice."] != accounts.profile_path_for_user("alice")


def test_hashed_profile_directory_cannot_alias_a_raw_user_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", tmp_path / "empty-legacy-project", raising=False)

    unsafe_path = accounts.profile_path_for_user("alice/bob")
    attacker_chosen_id = unsafe_path.parent.name

    assert attacker_chosen_id.startswith("~h-")
    assert accounts.profile_path_for_user(attacker_chosen_id) != unsafe_path


def test_profile_path_reuses_nonempty_legacy_user_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / "alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("alice") == legacy_profile


def test_profile_path_reuses_legacy_uppercase_user_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / "Alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("Alice") == legacy_profile


def test_case_distinct_users_do_not_share_an_uppercase_legacy_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy_project = tmp_path / "legacy-project"
    legacy_root = legacy_project / "data" / "users"
    legacy_profile = legacy_root / "Alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", legacy_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    uppercase = accounts.profile_path_for_user("Alice")
    lowercase = accounts.profile_path_for_user("alice")

    assert uppercase == legacy_profile
    assert lowercase == legacy_root / "~u-alice" / "playwright_profile"
    assert lowercase != uppercase


def test_profile_path_reuses_long_legacy_user_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_id = "a" * 97
    custom_root = tmp_path / "custom-users"
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / user_id / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user(user_id) == legacy_profile


def test_profile_path_ignores_non_profile_files_in_legacy_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / "alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "stale.tmp").write_text("not chromium data", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("alice") == (
        custom_root / "~u-alice" / "playwright_profile"
    )


def test_profile_path_uses_initialized_legacy_when_configured_directory_is_garbage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    canonical_profile = custom_root / "~u-alice" / "playwright_profile"
    canonical_profile.mkdir(parents=True)
    (canonical_profile / "stale.tmp").write_text("not chromium data", encoding="utf-8")
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / "alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("alice") == legacy_profile


def test_profile_path_prefers_nonempty_configured_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    canonical_profile = custom_root / "~u-alice" / "playwright_profile"
    canonical_profile.mkdir(parents=True)
    (canonical_profile / "Local State").write_text("canonical", encoding="utf-8")
    legacy_project = tmp_path / "legacy-project"
    legacy_profile = legacy_project / "data" / "users" / "alice" / "playwright_profile"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", tmp_path / "empty-single-profile")
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("alice") == canonical_profile


def test_default_profile_path_reuses_nonempty_single_user_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    custom_root = tmp_path / "custom-users"
    legacy_project = tmp_path / "legacy-project"
    empty_legacy_profile = (
        legacy_project / "data" / "users" / "default" / "playwright_profile"
    )
    empty_legacy_profile.mkdir(parents=True)
    single_user_profile = tmp_path / "single-user-profile"
    single_user_profile.mkdir()
    (single_user_profile / "Local State").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(settings, "user_data_root", custom_root)
    monkeypatch.setattr(settings, "playwright_profile_path", single_user_profile)
    monkeypatch.setattr(tenancy, "PROJECT_ROOT", legacy_project, raising=False)

    assert accounts.profile_path_for_user("default") == single_user_profile


def test_update_douyin_profile_stores_display_fields() -> None:
    with isolated_accounts_db() as conn:
        accounts.ensure_default_user()

        user = accounts.update_douyin_profile(
            "default",
            {
                "nickname": "抖音小号",
                "unique_id": "douyin_123",
                "sec_uid": "SEC_SELF",
                "avatar_url": "https://example.com/me.jpeg",
            },
        )

        assert user["douyin_nickname"] == "抖音小号"
        assert user["douyin_unique_id"] == "douyin_123"
        assert user["douyin_sec_uid"] == "SEC_SELF"
        assert user["douyin_avatar_url"] == "https://example.com/me.jpeg"
        row = conn.execute("SELECT display_name FROM users WHERE id = 'default'").fetchone()
        assert row["display_name"] == "本地默认用户"


def test_clear_douyin_profile_removes_account_fields_without_deleting_items() -> None:
    with isolated_accounts_db() as conn:
        accounts.ensure_default_user()
        accounts.update_douyin_profile(
            "default",
            {
                "nickname": "抖音小号",
                "unique_id": "douyin_123",
                "sec_uid": "SEC_SELF",
                "avatar_url": "https://example.com/me.jpeg",
            },
        )
        conn.execute(
            """
            INSERT INTO favorites (user_id, id, title, first_seen_at, last_seen_at)
            VALUES ('default', 'fav-1', '本地收藏', '2026-05-26', '2026-05-26')
            """
        )

        user = accounts.clear_douyin_profile("default")

        assert user["douyin_nickname"] is None
        assert user["douyin_unique_id"] is None
        assert user["douyin_sec_uid"] is None
        assert user["douyin_avatar_url"] is None
        assert user["douyin_profile_updated_at"] is None
        assert conn.execute("SELECT COUNT(*) AS c FROM favorites").fetchone()["c"] == 1


def test_list_douyin_accounts_returns_bound_enabled_users() -> None:
    with isolated_accounts_db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, douyin_unique_id,
                douyin_avatar_url, created_at
            ) VALUES (
                'alice', 'Alice', 'Alice抖音', 'alice_dy',
                'https://example.com/alice.jpg', '2026-05-26 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, created_at
            ) VALUES (
                'bob', 'Bob', NULL, '2026-05-26 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, display_name, douyin_nickname, disabled_at, created_at
            ) VALUES (
                'disabled', 'Disabled', 'Disabled抖音',
                '2026-05-27 00:00:00', '2026-05-26 00:00:00'
            )
            """
        )

        rows = accounts.list_douyin_accounts()

        assert [row["id"] for row in rows] == ["alice"]
        assert rows[0]["douyin_nickname"] == "Alice抖音"
        assert rows[0]["douyin_unique_id"] == "alice_dy"


def test_delete_user_data_removes_owned_rows_only() -> None:
    with isolated_accounts_db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
            CREATE TABLE likes_vec (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
            CREATE TABLE favorites_fts (id TEXT, user_id TEXT, title TEXT);
            CREATE TABLE likes_fts (id TEXT, user_id TEXT, title TEXT);
            """
        )
        for user_id in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users (id, display_name, created_at) VALUES (?, ?, '2026-05-26 00:00:00')",
                (user_id, user_id),
            )
            conn.execute(
                """
                INSERT INTO favorites (user_id, id, title, first_seen_at, last_seen_at)
                VALUES (?, 'shared', ?, '2026-05-26', '2026-05-26')
                """,
                (user_id, user_id),
            )
            conn.execute(
                """
                INSERT INTO likes (user_id, id, title, first_seen_at, last_seen_at)
                VALUES (?, 'shared', ?, '2026-05-26', '2026-05-26')
                """,
                (user_id, user_id),
            )
            conn.execute(
                """
                INSERT INTO recall_log (user_id, favorite_id, recalled_at)
                VALUES (?, 'shared', '2026-05-27')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO like_recall_log (user_id, like_id, recalled_at)
                VALUES (?, 'shared', '2026-05-27')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO uncollect_log (user_id, favorite_id, initiated_at, status)
                VALUES (?, 'shared', '2026-05-27', 'pending')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO unlike_log (user_id, like_id, initiated_at, status)
                VALUES (?, 'shared', '2026-05-27', 'pending')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO search_reindex_state (
                    user_id, content_kind, required_at, reason, completed_at
                ) VALUES (?, 'favorites', '2026-05-27', 'test', NULL)
                """,
                (user_id,),
            )
            conn.execute(
                "INSERT INTO favorites_vec (id, user_id) VALUES (?, ?)",
                (f"{user_id}:shared", user_id),
            )
            conn.execute(
                "INSERT INTO likes_vec (id, user_id) VALUES (?, ?)",
                (f"{user_id}:shared", user_id),
            )
            conn.execute(
                """
                INSERT INTO favorites_fts (id, user_id, title)
                VALUES (?, ?, ?)
                """,
                (f"{user_id}:shared", user_id, user_id),
            )
            conn.execute(
                """
                INSERT INTO likes_fts (id, user_id, title)
                VALUES (?, ?, ?)
                """,
                (f"{user_id}:shared", user_id, user_id),
            )

        accounts.delete_user_data("alice")

        rows = conn.execute("SELECT user_id, id FROM favorites ORDER BY user_id").fetchall()
        like_rows = conn.execute("SELECT user_id, id FROM likes ORDER BY user_id").fetchall()
        alice = conn.execute("SELECT disabled_at FROM users WHERE id = 'alice'").fetchone()
        bob = conn.execute("SELECT disabled_at FROM users WHERE id = 'bob'").fetchone()
        assert [(r["user_id"], r["id"]) for r in rows] == [("bob", "shared")]
        assert [(r["user_id"], r["id"]) for r in like_rows] == [("bob", "shared")]
        for table in (
            "recall_log",
            "like_recall_log",
            "uncollect_log",
            "unlike_log",
            "favorites_vec",
            "favorites_fts",
            "likes_vec",
            "likes_fts",
            "search_reindex_state",
        ):
            assert [
                row["user_id"]
                for row in conn.execute(f"SELECT user_id FROM {table} ORDER BY user_id").fetchall()
            ] == ["bob"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert alice["disabled_at"] is not None
        assert bob["disabled_at"] is None


if __name__ == "__main__":
    tests = [
        test_invite_claim_creates_user_and_session_without_storing_plain_code,
        test_invite_code_cannot_be_reused_after_single_claim,
        test_user_profile_paths_are_isolated,
        test_update_douyin_profile_stores_display_fields,
        test_clear_douyin_profile_removes_account_fields_without_deleting_items,
        test_list_douyin_accounts_returns_bound_enabled_users,
        test_delete_user_data_removes_owned_rows_only,
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

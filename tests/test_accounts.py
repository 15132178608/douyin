"""
Private-cloud account and invite tests.

Run:
    python tests/test_accounts.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from src import db
from src import accounts


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


def test_user_profile_paths_are_isolated() -> None:
    alice = accounts.profile_path_for_user("alice")
    bob = accounts.profile_path_for_user("bob")

    assert alice != bob
    assert str(alice).endswith("data\\users\\alice\\playwright_profile")
    assert str(bob).endswith("data\\users\\bob\\playwright_profile")


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


def test_delete_user_data_removes_owned_rows_only() -> None:
    with isolated_accounts_db() as conn:
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

        accounts.delete_user_data("alice")

        rows = conn.execute("SELECT user_id, id FROM favorites ORDER BY user_id").fetchall()
        alice = conn.execute("SELECT disabled_at FROM users WHERE id = 'alice'").fetchone()
        bob = conn.execute("SELECT disabled_at FROM users WHERE id = 'bob'").fetchone()
        assert [(r["user_id"], r["id"]) for r in rows] == [("bob", "shared")]
        assert alice["disabled_at"] is not None
        assert bob["disabled_at"] is None


if __name__ == "__main__":
    tests = [
        test_invite_claim_creates_user_and_session_without_storing_plain_code,
        test_invite_code_cannot_be_reused_after_single_claim,
        test_user_profile_paths_are_isolated,
        test_update_douyin_profile_stores_display_fields,
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

"""Private-cloud users, invite codes, sessions, and per-user paths."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import uuid
from pathlib import Path

from src.config import settings
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id, user_playwright_profile_path


class InviteError(ValueError):
    """Raised when an invite code cannot be claimed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def ensure_default_user() -> dict:
    conn = get_connection()
    now = _now()
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (DEFAULT_USER_ID, "本地默认用户", now),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (DEFAULT_USER_ID,)).fetchone()
    return dict(row)


def create_user(display_name: str, user_id: str | None = None) -> dict:
    conn = get_connection()
    clean_name = (display_name or "").strip() or "新用户"
    uid = normalize_user_id(user_id or uuid.uuid4().hex)
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        """,
        (uid, clean_name, _now()),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row)


def create_local_douyin_account() -> dict:
    """Create a local app user slot for another Douyin account."""
    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE disabled_at IS NULL"
    ).fetchone()["c"]
    return create_user(f"抖音账号 {int(count or 0) + 1}")


def get_user(user_id: str) -> dict | None:
    uid = normalize_user_id(user_id)
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ? AND disabled_at IS NULL",
        (uid,),
    ).fetchone()
    return dict(row) if row else None


def list_douyin_accounts() -> list[dict]:
    """Return enabled local user slots that have a saved Douyin profile."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE disabled_at IS NULL
          AND (
            NULLIF(douyin_nickname, '') IS NOT NULL
            OR NULLIF(douyin_unique_id, '') IS NOT NULL
            OR NULLIF(douyin_sec_uid, '') IS NOT NULL
            OR NULLIF(douyin_avatar_url, '') IS NOT NULL
          )
        ORDER BY COALESCE(douyin_profile_updated_at, created_at) DESC, created_at DESC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def update_douyin_profile(user_id: str, profile: dict) -> dict:
    """Save display-only Douyin account fields for the current web user."""
    uid = normalize_user_id(user_id)
    conn = get_connection()
    conn.execute(
        """
        UPDATE users
        SET douyin_nickname = COALESCE(NULLIF(?, ''), douyin_nickname),
            douyin_unique_id = COALESCE(NULLIF(?, ''), douyin_unique_id),
            douyin_sec_uid = COALESCE(NULLIF(?, ''), douyin_sec_uid),
            douyin_avatar_url = COALESCE(NULLIF(?, ''), douyin_avatar_url),
            douyin_profile_updated_at = ?
        WHERE id = ?
        """,
        (
            str(profile.get("nickname") or "").strip(),
            str(profile.get("unique_id") or "").strip(),
            str(profile.get("sec_uid") or "").strip(),
            str(profile.get("avatar_url") or "").strip(),
            _now(),
            uid,
        ),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row)


def clear_douyin_profile(user_id: str) -> dict:
    """Clear saved Douyin account display fields without deleting local content."""
    uid = normalize_user_id(user_id)
    conn = get_connection()
    conn.execute(
        """
        UPDATE users
        SET douyin_nickname = NULL,
            douyin_unique_id = NULL,
            douyin_sec_uid = NULL,
            douyin_avatar_url = NULL,
            douyin_profile_updated_at = NULL
        WHERE id = ?
        """,
        (uid,),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row)


def create_invite(
    created_by_user_id: str = DEFAULT_USER_ID,
    *,
    code: str | None = None,
    max_uses: int = 1,
    expires_at: datetime | None = None,
) -> str:
    ensure_default_user()
    invite_code = (code or secrets.token_urlsafe(18)).strip()
    if not invite_code:
        raise InviteError("邀请码不能为空")
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO invite_codes (
            code_hash, created_by_user_id, max_uses, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            _hash_secret(invite_code),
            normalize_user_id(created_by_user_id),
            max(1, int(max_uses or 1)),
            expires_at,
            _now(),
        ),
    )
    return invite_code


def claim_invite(code: str, display_name: str | None = None) -> tuple[dict, str]:
    invite_code = (code or "").strip()
    if not invite_code:
        raise InviteError("邀请码不能为空")

    conn = get_connection()
    code_hash = _hash_secret(invite_code)
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT *
            FROM invite_codes
            WHERE code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if row is None:
            raise InviteError("邀请码不存在")
        if row["disabled_at"] is not None:
            raise InviteError("邀请码已停用")
        if row["expires_at"] is not None and _parse_datetime(row["expires_at"]) <= now:
            raise InviteError("邀请码已过期")
        if int(row["used_count"] or 0) >= int(row["max_uses"] or 1):
            raise InviteError("邀请码已被使用")

        # User/session creation stays inside the same write transaction. If the
        # conditional invite claim loses a race, the new rows are rolled back.
        user = create_user(display_name or "新用户")
        updated = conn.execute(
            """
            UPDATE invite_codes
            SET used_count = used_count + 1,
                claimed_by_user_id = ?
            WHERE code_hash = ?
              AND disabled_at IS NULL
              AND used_count < max_uses
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (user["id"], code_hash, now),
        )
        if updated.rowcount != 1:
            raise InviteError("邀请码已被使用")
        token = create_session(user["id"])
        conn.execute("COMMIT")
        return user, token
    except Exception:
        conn.execute("ROLLBACK")
        raise


def create_session(user_id: str, days: int | None = None) -> str:
    conn = get_connection()
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=days or settings.session_days)
    conn.execute(
        """
        INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (_hash_secret(token), normalize_user_id(user_id), now, expires),
    )
    return token


def user_from_session(token: str | None) -> dict | None:
    if not token:
        return None
    conn = get_connection()
    row = conn.execute(
        """
        SELECT u.*
        FROM web_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
          AND s.revoked_at IS NULL
          AND s.expires_at > ?
          AND u.disabled_at IS NULL
        """,
        (_hash_secret(token), _now()),
    ).fetchone()
    return dict(row) if row else None


def revoke_session(token: str | None) -> None:
    if not token:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ?",
        (_now(), _hash_secret(token)),
    )


def profile_path_for_user(user_id: str | None) -> Path:
    return user_playwright_profile_path(user_id)


def delete_user_data(user_id: str) -> None:
    """Remove one user's database-owned data and disable the user row."""
    uid = normalize_user_id(user_id)
    if uid == DEFAULT_USER_ID:
        raise ValueError("默认用户不能通过这个入口删除")
    conn = get_connection()
    now = _now()
    with conn:
        conn.execute("BEGIN")
        try:
            for table in (
                "recall_log",
                "like_recall_log",
                "uncollect_log",
                "unlike_log",
                "favorites_vec",
                "favorites_fts",
                "likes_vec",
                "likes_fts",
                "favorites",
                "likes",
                "crawl_runs",
                "like_crawl_runs",
                "categories",
                "like_categories",
                "search_reindex_state",
                "job_queue",
                "web_sessions",
            ):
                column = "account_id" if table in {"categories", "like_categories"} else "user_id"
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (uid,))
            conn.execute(
                "UPDATE invite_codes SET disabled_at = ? WHERE claimed_by_user_id = ?",
                (now, uid),
            )
            conn.execute(
                "UPDATE users SET disabled_at = ? WHERE id = ?",
                (now, uid),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

"""Read-only first-run status aggregation for the local Web setup flow."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.config import settings
from src.content.kinds import get_content_kind, list_content_kinds
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id, user_playwright_profile_path


def _count_items(content_kind: str, user_id: str) -> int:
    kind = get_content_kind(content_kind)
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.table} WHERE user_id = ? AND is_removed = 0",
        (normalize_user_id(user_id),),
    ).fetchone()
    return int(row["c"] or 0)


def _count_indexed_items(content_kind: str, user_id: str) -> int:
    kind = get_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM {kind.table} item
            JOIN {kind.vector_table} vec
              ON vec.id = (? || ':' || item.id)
              OR (? = 'default' AND vec.id = item.id)
            WHERE item.user_id = ? AND item.is_removed = 0
            """,
            (uid, uid, uid),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["c"] or 0)


def _content_status(content_kind: str, user_id: str) -> dict[str, Any]:
    kind = get_content_kind(content_kind)
    total = _count_items(kind.key, user_id)
    indexed = _count_indexed_items(kind.key, user_id)
    return {
        "key": kind.key,
        "label": kind.label,
        "total": total,
        "indexed": indexed,
        "needs_index": total > indexed,
    }


def _profile_status(user_id: str) -> dict[str, Any]:
    uid = normalize_user_id(user_id)
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return {
            "nickname": None,
            "unique_id": None,
            "avatar_url": None,
            "updated_at": None,
        }
    return {
        "nickname": row["douyin_nickname"],
        "unique_id": row["douyin_unique_id"],
        "avatar_url": row["douyin_avatar_url"],
        "updated_at": row["douyin_profile_updated_at"],
    }


def _job_summary(user_id: str) -> dict[str, int | bool]:
    uid = normalize_user_id(user_id)
    conn = get_connection()
    summary: dict[str, int | bool] = {
        "pending": 0,
        "running": 0,
        "failed": 0,
        "success": 0,
        "total": 0,
        "needs_attention": False,
    }
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS c
        FROM job_queue
        WHERE user_id = ?
        GROUP BY status
        """,
        (uid,),
    ).fetchall()
    total = 0
    for row in rows:
        status = str(row["status"] or "")
        count = int(row["c"] or 0)
        total += count
        if status in summary:
            summary[status] = count
    summary["total"] = total
    summary["needs_attention"] = int(summary["running"]) > 0 or int(summary["failed"]) > 0
    return summary


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def get_onboarding_status(user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    """Return the current user's first-run setup state without side effects."""
    uid = normalize_user_id(user_id)
    content = {
        kind.key: _content_status(kind.key, uid)
        for kind in list_content_kinds()
    }
    profile = _profile_status(uid)
    has_saved_profile = any(
        bool(profile.get(key))
        for key in ("nickname", "unique_id", "avatar_url")
    )
    profile_path = user_playwright_profile_path(uid)
    has_profile = has_saved_profile or _path_exists(profile_path)
    has_any_items = any(int(data["total"]) > 0 for data in content.values())
    return {
        "user_id": uid,
        "needs_setup": not has_any_items,
        "has_any_items": has_any_items,
        "has_profile": has_profile,
        "profile_path_exists": _path_exists(profile_path),
        "database_path_exists": _path_exists(settings.db_path),
        "profile": profile,
        "favorites": content["favorites"],
        "likes": content["likes"],
        "jobs": _job_summary(uid),
    }

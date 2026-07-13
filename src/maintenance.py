"""Maintenance status aggregation and long-running local upkeep helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat as stat_module
import tempfile
from typing import Any
import uuid

from loguru import logger

from src import db as db_module
from src import jobs
from src import server_runtime
from src import update_check
from src.config import PROJECT_ROOT, settings
from src.content.kinds import get_content_kind, list_content_kinds
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id, user_playwright_profile_path


DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "exports"
DEFAULT_BACKUP_RETENTION_KEEP = 8
REQUIRED_RESTORE_TABLES = {
    "users",
    "favorites",
    "likes",
    "job_queue",
    "crawl_runs",
    "like_crawl_runs",
}
MANIFEST_REQUIRED_COUNT_TABLES = {"users", "favorites", "likes"}
ORDINARY_BACKUP_PATTERN = "recall-backup-*.db"
PROTECTED_BACKUP_PATTERNS = (
    "pre-install-recall-*.db",
    "pre-restore-recall-*.db",
    "pre-release-recall-*.db",
)
RECOVERY_BACKUP_PATTERNS = (ORDINARY_BACKUP_PATTERN, *PROTECTED_BACKUP_PATTERNS)
BACKUP_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})")
MIGRATION_PRESERVED_TABLES = (
    "users",
    "favorites",
    "likes",
    "job_queue",
    "crawl_runs",
    "like_crawl_runs",
    "recall_log",
    "like_recall_log",
    "uncollect_log",
    "unlike_log",
)
CURRENT_SCHEMA_COLUMNS = {
    "users": {
        "id", "display_name", "created_at", "disabled_at", "douyin_nickname",
        "douyin_unique_id", "douyin_sec_uid", "douyin_avatar_url",
        "douyin_profile_updated_at",
    },
    "favorites": {
        "user_id", "id", "title", "description", "author", "author_id",
        "video_url", "cover_url", "duration_ms", "favorited_at", "first_seen_at",
        "last_seen_at", "last_recalled_at", "user_note", "raw_json", "is_removed",
        "discovery_index", "video_tags", "video_created_at", "digg_count",
        "category_id", "llm_tags",
    },
    "likes": {
        "user_id", "id", "title", "description", "author", "author_id",
        "video_url", "cover_url", "duration_ms", "liked_at", "first_seen_at",
        "last_seen_at", "last_recalled_at", "user_note", "raw_json", "is_removed",
        "discovery_index", "video_tags", "video_created_at", "digg_count",
        "category_id", "llm_tags",
    },
    "recall_log": {
        "id", "user_id", "favorite_id", "recalled_at", "channel", "user_action",
    },
    "like_recall_log": {
        "id", "user_id", "like_id", "recalled_at", "channel", "user_action",
    },
    "uncollect_log": {
        "id", "user_id", "favorite_id", "initiated_at", "finished_at", "status",
        "channel", "error_message",
    },
    "unlike_log": {
        "id", "user_id", "like_id", "initiated_at", "finished_at", "status",
        "channel", "error_message",
    },
    "crawl_runs": {
        "id", "started_at", "finished_at", "status", "new_count", "updated_count",
        "removed_count", "error_message", "user_id",
    },
    "like_crawl_runs": {
        "id", "started_at", "finished_at", "status", "new_count", "updated_count",
        "removed_count", "error_message", "user_id",
    },
    "categories": {
        "id", "account_id", "name", "auto_name", "keywords_json", "item_count",
        "centroid_blob", "algo", "created_at", "updated_at",
    },
    "like_categories": {
        "id", "account_id", "name", "auto_name", "keywords_json", "item_count",
        "centroid_blob", "algo", "created_at", "updated_at",
    },
    "job_queue": {
        "id", "user_id", "kind", "payload_json", "status", "attempts",
        "max_attempts", "created_at", "started_at", "finished_at", "error_message",
        "next_run_at",
    },
    "invite_codes": {
        "code_hash", "created_by_user_id", "claimed_by_user_id", "max_uses",
        "used_count", "expires_at", "created_at", "disabled_at",
    },
    "web_sessions": {
        "token_hash", "user_id", "created_at", "expires_at", "revoked_at",
    },
    "search_reindex_state": {
        "user_id", "content_kind", "required_at", "reason", "completed_at",
    },
    "login_rate_limits": {
        "scope", "subject_hash", "window_started_at", "failed_count",
        "blocked_until", "updated_at",
    },
}
CURRENT_PRIMARY_KEYS = {
    "users": ("id",),
    "invite_codes": ("code_hash",),
    "web_sessions": ("token_hash",),
    "favorites": ("user_id", "id"),
    "likes": ("user_id", "id"),
    "recall_log": ("id",),
    "like_recall_log": ("id",),
    "crawl_runs": ("id",),
    "like_crawl_runs": ("id",),
    "uncollect_log": ("id",),
    "unlike_log": ("id",),
    "job_queue": ("id",),
    "search_reindex_state": ("user_id", "content_kind"),
    "login_rate_limits": ("scope", "subject_hash"),
    "categories": ("id",),
    "like_categories": ("id",),
}
CURRENT_FOREIGN_KEY_CONSTRAINTS = {
    "invite_codes": (
        ("users", (("claimed_by_user_id", "id"),)),
        ("users", (("created_by_user_id", "id"),)),
    ),
    "web_sessions": (("users", (("user_id", "id"),)),),
    "favorites": (("users", (("user_id", "id"),)),),
    "likes": (("users", (("user_id", "id"),)),),
    "recall_log": (
        ("favorites", (("user_id", "user_id"), ("favorite_id", "id"))),
    ),
    "like_recall_log": (
        ("likes", (("user_id", "user_id"), ("like_id", "id"))),
    ),
    "uncollect_log": (
        ("favorites", (("user_id", "user_id"), ("favorite_id", "id"))),
    ),
    "unlike_log": (
        ("likes", (("user_id", "user_id"), ("like_id", "id"))),
    ),
    "job_queue": (("users", (("user_id", "id"),)),),
    "search_reindex_state": (("users", (("user_id", "id"),)),),
}
CURRENT_LOG_FOREIGN_KEYS = {
    "recall_log": ("favorites", "favorite_id"),
    "like_recall_log": ("likes", "like_id"),
    "uncollect_log": ("favorites", "favorite_id"),
    "unlike_log": ("likes", "like_id"),
}
DOUYIN_AUTH_ERROR_MARKERS = (
    "用户未登录",
    "登录态失效",
    "请登录",
    "请先登录",
    "login required",
    "not login",
    "not logged in",
    "unauthenticated",
)
DOUYIN_AUTH_JOB_KINDS = {
    "sync_favorites",
    "sync_likes",
    "uncollect",
}


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: str
    size_bytes: int
    modified_at: str


@dataclass(frozen=True)
class RestoreResult:
    backup_path: Path
    restored_path: Path
    safety_backup_path: Path | None
    validation: dict
    cleanup_warnings: tuple[str, ...] = ()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _backup_dir(output_dir: Path | None = None) -> Path:
    return Path(output_dir) if output_dir is not None else DEFAULT_BACKUP_DIR


def list_sqlite_backups(output_dir: Path | None = None, *, limit: int = 8) -> list[BackupInfo]:
    """Return recent SQLite backups from the top-level backup directory."""
    root = _backup_dir(output_dir)
    if not root.exists():
        return []
    files = [p for p in root.glob("recall-backup-*.db") if p.is_file()]
    files.sort(key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
    items: list[BackupInfo] = []
    for path in files[: max(1, int(limit or 1))]:
        stat = path.stat()
        items.append(
            BackupInfo(
                name=path.name,
                path=str(path),
                size_bytes=int(stat.st_size),
                modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            )
        )
    return items


def _backup_sort_key(path: Path) -> tuple[str, float, str]:
    timestamp = BACKUP_TIMESTAMP_RE.search(path.name)
    return (
        timestamp.group(1) if timestamp else "",
        path.stat().st_mtime,
        path.name,
    )


def _backup_info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(
        name=path.name,
        path=str(path),
        size_bytes=int(stat.st_size),
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    )


def _ordinary_backup_paths(output_dir: Path | None = None) -> list[Path]:
    root = _backup_dir(output_dir)
    if not root.exists():
        return []
    files = [path for path in root.glob(ORDINARY_BACKUP_PATTERN) if path.is_file()]
    files.sort(key=_backup_sort_key, reverse=True)
    return files


def _protected_backup_paths(output_dir: Path | None = None) -> list[Path]:
    root = _backup_dir(output_dir)
    if not root.exists():
        return []
    files_by_path: dict[Path, Path] = {}
    for pattern in PROTECTED_BACKUP_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                files_by_path[path.resolve()] = path
    files = list(files_by_path.values())
    files.sort(key=_backup_sort_key, reverse=True)
    return files


def describe_backup_retention(
    output_dir: Path | None = None,
    *,
    keep_latest: int = DEFAULT_BACKUP_RETENTION_KEEP,
) -> dict:
    """Return the retention plan without deleting files."""
    keep = max(1, int(keep_latest or DEFAULT_BACKUP_RETENTION_KEEP))
    ordinary = _ordinary_backup_paths(output_dir)
    protected = _protected_backup_paths(output_dir)
    keepers = ordinary[:keep]
    candidates = ordinary[keep:]
    return {
        "keep_latest": keep,
        "ordinary_pattern": ORDINARY_BACKUP_PATTERN,
        "protected_patterns": list(PROTECTED_BACKUP_PATTERNS),
        "ordinary_count": len(ordinary),
        "protected_count": len(protected),
        "kept": [_backup_info(path).__dict__ for path in keepers],
        "delete_candidates": [_backup_info(path).__dict__ for path in candidates],
        "protected": [_backup_info(path).__dict__ for path in protected],
        "delete_method": "one_file_at_a_time",
    }


def enforce_backup_retention(
    output_dir: Path | None = None,
    *,
    keep_latest: int = DEFAULT_BACKUP_RETENTION_KEEP,
) -> dict:
    """Delete old ordinary SQLite backups one explicit file path at a time."""
    plan = describe_backup_retention(output_dir, keep_latest=keep_latest)
    deleted: list[dict] = []
    errors: list[dict] = []
    for item in plan["delete_candidates"]:
        path = Path(item["path"])
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted.append(item)
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    refreshed = describe_backup_retention(output_dir, keep_latest=keep_latest)
    return {
        "ok": not errors,
        "keep_latest": plan["keep_latest"],
        "ordinary_pattern": plan["ordinary_pattern"],
        "protected_patterns": plan["protected_patterns"],
        "deleted": deleted,
        "errors": errors,
        "kept": refreshed["kept"],
        "protected": refreshed["protected"],
        "ordinary_count": refreshed["ordinary_count"],
        "protected_count": refreshed["protected_count"],
        "delete_method": "one_file_at_a_time",
    }


def list_recovery_backups(output_dir: Path | None = None, *, limit: int = 8) -> list[BackupInfo]:
    """Return recent user-created and installer-created recovery backups."""
    root = _backup_dir(output_dir)
    if not root.exists():
        return []
    files_by_path: dict[Path, Path] = {}
    for pattern in RECOVERY_BACKUP_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                files_by_path[path.resolve()] = path
    files = list(files_by_path.values())
    files.sort(key=_backup_sort_key, reverse=True)
    items: list[BackupInfo] = []
    for path in files[: max(1, int(limit or 1))]:
        items.append(_backup_info(path))
    return items


def _sqlite_row_to_dict(row: Any | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _looks_like_douyin_auth_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(marker.lower() in text for marker in DOUYIN_AUTH_ERROR_MARKERS)


def _douyin_auth_error_snippet(message: Any, *, max_chars: int = 240) -> str:
    text = str(message or "").strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first_line) <= max_chars:
        return first_line
    return first_line[: max_chars - 1].rstrip() + "…"


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _douyin_profile_summary(user_id: str) -> dict:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT douyin_nickname, douyin_unique_id, douyin_sec_uid,
               douyin_avatar_url, douyin_profile_updated_at
        FROM users
        WHERE id = ?
        """,
        (normalize_user_id(user_id),),
    ).fetchone()
    if row is None:
        return {
            "nickname": None,
            "unique_id": None,
            "sec_uid": None,
            "avatar_url": None,
            "updated_at": None,
        }
    return {
        "nickname": row["douyin_nickname"],
        "unique_id": row["douyin_unique_id"],
        "sec_uid": row["douyin_sec_uid"],
        "avatar_url": row["douyin_avatar_url"],
        "updated_at": row["douyin_profile_updated_at"],
    }


def _douyin_auth_recovery_summary(
    user_id: str,
    crawl_runs: dict[str, dict],
    job_items: list[dict] | None = None,
) -> dict:
    uid = normalize_user_id(user_id)
    errors: list[dict] = []

    for job in (job_items if job_items is not None else jobs.list_jobs(user_id=uid, limit=50)):
        if job.get("status") != "failed":
            continue
        kind = str(job.get("kind") or "")
        if kind not in DOUYIN_AUTH_JOB_KINDS:
            continue
        message = str(job.get("error_message") or "")
        if not _looks_like_douyin_auth_error(message):
            continue
        errors.append(
            {
                "source": kind,
                "message": _douyin_auth_error_snippet(message),
                "at": job.get("finished_at") or job.get("created_at"),
            }
        )

    for key, run in crawl_runs.items():
        latest = run.get("latest") or {}
        if latest.get("status") != "failed":
            continue
        message = str(latest.get("error_message") or "")
        if not _looks_like_douyin_auth_error(message):
            continue
        errors.append(
            {
                "source": f"{key}_crawl",
                "message": _douyin_auth_error_snippet(message),
                "at": latest.get("finished_at") or latest.get("started_at"),
            }
        )

    profile = _douyin_profile_summary(uid)
    profile_path_exists = _path_exists(user_playwright_profile_path(uid))
    has_saved_profile = any(
        bool(profile.get(key))
        for key in ("nickname", "unique_id", "sec_uid", "avatar_url")
    )
    has_local_profile = bool(has_saved_profile or profile_path_exists)
    needs_rebind = bool(errors)
    if needs_rebind:
        status = "expired"
    elif has_local_profile:
        status = "bound"
    else:
        status = "missing"

    return {
        "status": status,
        "needs_rebind": needs_rebind,
        "recovery_url": "/auth",
        "profile_path_exists": profile_path_exists,
        "has_saved_profile": has_saved_profile,
        "profile": profile,
        "latest_error": errors[0] if errors else None,
        "errors": errors[:3],
    }


def _content_summary(user_id: str, content_kind: str) -> dict:
    kind = get_content_kind(content_kind)
    conn = get_connection()
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {kind.table} WHERE user_id = ? AND is_removed = 0",
            (user_id,),
        ).fetchone()[0]
    )
    indexed = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {kind.vector_table} WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    return {
        "key": kind.key,
        "label": kind.label,
        "total": total,
        "indexed": indexed,
        "needs_index": total > indexed,
    }


def _crawl_run_summary(user_id: str, content_kind: str) -> dict:
    kind = get_content_kind(content_kind)
    conn = get_connection()
    latest = conn.execute(
        f"""
        SELECT started_at, finished_at, status, new_count, updated_count,
               removed_count, error_message
        FROM {kind.crawl_runs_table}
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    latest_success = conn.execute(
        f"""
        SELECT started_at, finished_at, status, new_count, updated_count,
               removed_count, error_message
        FROM {kind.crawl_runs_table}
        WHERE user_id = ? AND status = 'success'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return {
        "key": kind.key,
        "label": kind.label,
        "latest": _sqlite_row_to_dict(latest),
        "latest_success": _sqlite_row_to_dict(latest_success),
    }


def _is_retrying_job(job: dict) -> bool:
    try:
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(job.get("max_attempts") or 1))
    except (TypeError, ValueError):
        return False
    return (
        job.get("status") == "pending"
        and attempts < max_attempts
        and bool(job.get("error_message"))
        and bool(job.get("next_run_at"))
    )


def _job_summary(job_items: list[dict], *, recovered_stale_running: int = 0) -> dict:
    summary = {
        "pending": 0,
        "running": 0,
        "failed": 0,
        "success": 0,
        "total": 0,
        "retrying": 0,
        "next_run_at": None,
        "recovered_stale_running": max(0, int(recovered_stale_running or 0)),
    }
    for job in job_items:
        summary["total"] += 1
        status = job.get("status")
        if status in summary:
            summary[status] += 1
        if _is_retrying_job(job):
            summary["retrying"] += 1
            next_run_at = job.get("next_run_at")
            if next_run_at is not None and (
                summary["next_run_at"] is None
                or str(next_run_at) < str(summary["next_run_at"])
            ):
                summary["next_run_at"] = next_run_at
    summary["needs_attention"] = summary["failed"] > 0 or summary["running"] > 0
    return summary


def _section(status: str, ok: bool, message: str, details: dict) -> dict:
    return {
        "status": status,
        "ok": bool(ok),
        "message": message,
        "details": details,
    }


def _job_item_for_status(job: dict) -> dict:
    try:
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(job.get("max_attempts") or 1))
    except (TypeError, ValueError):
        attempts = 0
        max_attempts = 1
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "attempts": attempts,
        "max_attempts": max_attempts,
        "can_retry": bool(job.get("status") == "pending" and attempts < max_attempts),
        "next_run_at": job.get("next_run_at"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "error_message": job.get("error_message"),
        "payload": job.get("payload") or {},
    }


def _failed_job_items(job_items: list[dict], *, limit: int = 10) -> list[dict]:
    out: list[dict] = []
    for job in job_items:
        if job.get("status") != "failed":
            continue
        out.append(_job_item_for_status(job))
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _retrying_job_items(job_items: list[dict], *, limit: int = 10) -> list[dict]:
    out: list[dict] = []
    for job in job_items:
        if not _is_retrying_job(job):
            continue
        out.append(_job_item_for_status(job))
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _service_section(server_status: dict) -> dict:
    state = str(server_status.get("state") or "unknown")
    return _section(
        state,
        state != "stale",
        str(server_status.get("message") or "服务状态未知。"),
        dict(server_status),
    )


def _login_section(auth_summary: dict) -> dict:
    status = str(auth_summary.get("status") or "missing")
    if auth_summary.get("needs_rebind"):
        message = "抖音登录态可能过期，需要重新绑定。"
    elif status == "bound":
        message = "抖音账号已绑定。"
    else:
        message = "尚未绑定抖音账号。"
    return _section(
        status,
        not bool(auth_summary.get("needs_rebind")),
        message,
        dict(auth_summary),
    )


def _failed_tasks_section(job_items: list[dict], job_summary: dict) -> dict:
    failed_count = int(job_summary.get("failed") or 0)
    items = _failed_job_items(job_items)
    retrying_items = _retrying_job_items(job_items)
    return _section(
        "failed" if failed_count else "ok",
        failed_count == 0,
        f"有 {failed_count} 个失败任务需要处理。" if failed_count else "没有失败任务。",
        {
            "count": failed_count,
            "items": items,
            "retrying_count": len(retrying_items),
            "retrying_items": retrying_items,
            "next_run_at": job_summary.get("next_run_at"),
            "recovered_stale_running": int(job_summary.get("recovered_stale_running") or 0),
        },
    )


def _backup_section(backups: dict) -> dict:
    count = int(backups.get("count") or 0)
    latest = backups.get("latest")
    return _section(
        "ok" if latest else "missing",
        bool(latest),
        f"最近备份：{latest['name']}" if latest else "没有可用 SQLite 备份。",
        dict(backups),
    )


def _index_section(contents: dict[str, dict]) -> dict:
    needs_index = [
        key
        for key, summary in contents.items()
        if summary.get("needs_index")
    ]
    return _section(
        "needs_index" if needs_index else "ok",
        not needs_index,
        "有内容需要补建搜索索引。" if needs_index else "搜索索引已覆盖当前内容。",
        {
            "needs_index": needs_index,
            "contents": contents,
        },
    )


def _actions_section(actions: list[dict]) -> dict:
    return _section(
        "attention" if actions else "ok",
        not bool(actions),
        f"有 {len(actions)} 个建议动作。" if actions else "暂无建议动作。",
        {"items": actions},
    )


def _maintenance_sections(
    *,
    server_status: dict,
    auth_summary: dict,
    job_summary: dict,
    job_items: list[dict],
    backups: dict,
    contents: dict[str, dict],
    actions: list[dict],
) -> dict:
    return {
        "service": _service_section(server_status),
        "login": _login_section(auth_summary),
        "failed_tasks": _failed_tasks_section(job_items, job_summary),
        "backup": _backup_section(backups),
        "index": _index_section(contents),
        "actions": _actions_section(actions),
    }


def _suggested_actions(attention_codes: list[str]) -> list[dict]:
    actions: list[dict] = []

    def add(code: str, label: str, description: str, target: str, severity: str = "warning") -> None:
        actions.append(
            {
                "code": code,
                "label": label,
                "description": description,
                "target": target,
                "severity": severity,
            }
        )

    if "failed_jobs" in attention_codes:
        add("review_failed_jobs", "查看失败任务", "后台队列里有失败任务，需要查看错误原因。", "/maintenance", "critical")
    if "douyin_login_expired" in attention_codes:
        add("rebind_douyin", "重新绑定抖音账号", "抖音登录态可能过期，重新扫码后再同步。", "/auth", "critical")
    if "no_backups" in attention_codes:
        add("create_backup", "立即备份", "当前没有可用 SQLite 备份，建议先生成一份。", "/maintenance/backup")
    if "favorites_needs_index" in attention_codes:
        add("index_favorites", "索引收藏", "收藏数量多于索引数量，需要补建搜索索引。", "/jobs/index?kind=favorites")
    if "likes_needs_index" in attention_codes:
        add("index_likes", "索引喜欢", "喜欢数量多于索引数量，需要补建搜索索引。", "/jobs/index?kind=likes")
    if "latest_favorites_crawl_failed" in attention_codes:
        add("retry_favorites_sync", "重试收藏同步", "最近一次收藏同步失败，修复登录或网络后重试。", "/jobs/sync?kind=favorites")
    if "latest_likes_crawl_failed" in attention_codes:
        add("retry_likes_sync", "重试喜欢同步", "最近一次喜欢同步失败，修复登录或网络后重试。", "/jobs/sync?kind=likes")
    return actions


def _capability_base(section: dict) -> dict:
    return {
        "status": str(section.get("status") or "unknown"),
        "ok": bool(section.get("ok")),
        "message": str(section.get("message") or ""),
    }


def _maintenance_capabilities(sections: dict, suggested_actions: list[dict]) -> dict:
    service_section = sections["service"]
    login_section = sections["login"]
    failed_section = sections["failed_tasks"]
    backup_section = sections["backup"]
    index_section = sections["index"]

    service_details = dict(service_section.get("details") or {})
    login_details = dict(login_section.get("details") or {})
    failed_details = dict(failed_section.get("details") or {})
    backup_details = dict(backup_section.get("details") or {})
    index_details = dict(index_section.get("details") or {})

    return {
        "service_status": {
            **_capability_base(service_section),
            "details": service_details,
        },
        "login_status": {
            **_capability_base(login_section),
            "needs_rebind": bool(login_details.get("needs_rebind")),
            "recovery_url": login_details.get("recovery_url"),
            "profile": login_details.get("profile") or {},
            "latest_error": login_details.get("latest_error"),
            "details": login_details,
        },
        "failed_tasks": {
            **_capability_base(failed_section),
            "count": int(failed_details.get("count") or 0),
            "items": failed_details.get("items") or [],
            "retrying_count": int(failed_details.get("retrying_count") or 0),
            "retrying_items": failed_details.get("retrying_items") or [],
            "next_run_at": failed_details.get("next_run_at"),
            "recovered_stale_running": int(failed_details.get("recovered_stale_running") or 0),
            "details": failed_details,
        },
        "backup_status": {
            **_capability_base(backup_section),
            "count": int(backup_details.get("count") or 0),
            "latest": backup_details.get("latest"),
            "items": backup_details.get("items") or [],
            "output_dir": backup_details.get("output_dir"),
            "retention": backup_details.get("retention") or {},
            "details": backup_details,
        },
        "index_status": {
            **_capability_base(index_section),
            "needs_index": index_details.get("needs_index") or [],
            "contents": index_details.get("contents") or {},
            "details": index_details,
        },
        "suggested_actions": [dict(action) for action in suggested_actions],
    }


def get_maintenance_status(
    user_id: str = DEFAULT_USER_ID,
    *,
    backup_dir: Path | None = None,
    include_update: bool = False,
    update_status_getter=None,
) -> dict:
    uid = normalize_user_id(user_id)
    backups = list_sqlite_backups(backup_dir)
    backup_root = _backup_dir(backup_dir)
    retention = describe_backup_retention(backup_root)
    contents = {kind.key: _content_summary(uid, kind.key) for kind in list_content_kinds()}
    crawl_runs = {kind.key: _crawl_run_summary(uid, kind.key) for kind in list_content_kinds()}
    recovered_stale_running = jobs.recover_stale_running_jobs(user_id=uid)
    job_items = jobs.list_jobs(user_id=uid, limit=200, recover_stale=False)
    job_summary = _job_summary(job_items, recovered_stale_running=recovered_stale_running)
    auth_summary = _douyin_auth_recovery_summary(uid, crawl_runs, job_items)

    attention_codes: list[str] = []
    if job_summary["failed"] > 0:
        attention_codes.append("failed_jobs")
    if auth_summary["needs_rebind"]:
        attention_codes.append("douyin_login_expired")
    if not backups:
        attention_codes.append("no_backups")
    for key, run in crawl_runs.items():
        latest = run.get("latest") or {}
        if latest.get("status") == "failed":
            attention_codes.append(f"latest_{key}_crawl_failed")
    for key, summary in contents.items():
        if summary["needs_index"]:
            attention_codes.append(f"{key}_needs_index")

    update_status = None
    if include_update:
        getter = update_status_getter or update_check.get_cached_update_status
        update_status = getter()

    server_status = server_runtime.get_server_status()
    backup_status = {
        "output_dir": str(backup_root),
        "count": len(backups),
        "latest": backups[0].__dict__ if backups else None,
        "items": [item.__dict__ for item in backups],
        "retention": retention,
    }
    suggested_actions = _suggested_actions(attention_codes)
    sections = _maintenance_sections(
        server_status=server_status,
        auth_summary=auth_summary,
        job_summary=job_summary,
        job_items=job_items,
        backups=backup_status,
        contents=contents,
        actions=suggested_actions,
    )

    return {
        "schema_version": 1,
        "capabilities_schema_version": 1,
        "user_id": uid,
        "contents": contents,
        "favorites": contents["favorites"],
        "likes": contents["likes"],
        "crawl_runs": crawl_runs,
        "jobs": job_summary,
        "auth": auth_summary,
        "server": server_status,
        "backups": backup_status,
        "update": update_status,
        "attention_codes": attention_codes,
        "suggested_actions": suggested_actions,
        "needs_attention": bool(attention_codes),
        "sections": sections,
        "capabilities": _maintenance_capabilities(sections, suggested_actions),
    }


def enqueue_full_maintenance(
    user_id: str = DEFAULT_USER_ID,
    *,
    max_pages: int = 500,
    backup_dir: Path | None = None,
) -> list[int]:
    """Queue the standard local upkeep chain in FIFO order."""
    uid = normalize_user_id(user_id)
    pages = max(1, int(max_pages or 500))
    output_dir = str(_backup_dir(backup_dir))
    return [
        jobs.enqueue_job("sync_favorites", user_id=uid, payload={"max_pages": pages}),
        jobs.enqueue_job("sync_likes", user_id=uid, payload={"max_pages": pages}),
        jobs.enqueue_job("index", user_id=uid, payload={"content_kind": "favorites"}),
        jobs.enqueue_job("index", user_id=uid, payload={"content_kind": "likes"}),
        jobs.enqueue_job("backup_sqlite", user_id=uid, payload={"output_dir": output_dir}),
    ]


def create_sqlite_backup(output_dir: Path | None = None):
    from src import exporter

    root = _backup_dir(output_dir)
    result = exporter.backup_sqlite(root)
    enforce_backup_retention(root)
    return result


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_sqlite_readonly_uri(path), uri=True)


def _sqlite_backup_snapshot(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = _open_sqlite_readonly(source_path)
    try:
        destination = sqlite3.connect(str(destination_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _sidecar_info(path: Path, suffix: str) -> dict:
    sidecar = Path(str(path) + suffix)
    try:
        stat = sidecar.stat()
    except FileNotFoundError:
        return {"path": str(sidecar), "exists": False, "size_bytes": 0}
    is_file = stat_module.S_ISREG(stat.st_mode)
    return {
        "path": str(sidecar),
        "exists": is_file,
        "size_bytes": stat.st_size if is_file else 0,
    }


def _consolidate_sqlite_file(path: Path) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise RuntimeError(f"目标数据库 WAL checkpoint 仍忙：{tuple(checkpoint)}")
        journal_mode = str(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise RuntimeError(f"无法把目标数据库切换到自包含模式：{journal_mode}")
    finally:
        conn.close()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_sqlite_identifier(table)})").fetchall()
    }


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_sqlite_identifier(table)})").fetchall()
    return [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5])]


def _foreign_key_constraints(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    rows = conn.execute(f"PRAGMA foreign_key_list({_sqlite_identifier(table)})").fetchall()
    grouped: dict[int, tuple[str, list[tuple[int, str, str]]]] = {}
    for row in rows:
        constraint_id = int(row[0])
        parent = str(row[2])
        if constraint_id not in grouped:
            grouped[constraint_id] = (parent, [])
        grouped_parent, columns = grouped[constraint_id]
        if grouped_parent != parent:
            return (("<invalid>", ()),)
        columns.append((int(row[1]), str(row[3]), str(row[4])))
    constraints = [
        (
            parent,
            tuple((source, target) for _seq, source, target in sorted(columns)),
        )
        for parent, columns in grouped.values()
    ]
    return tuple(sorted(constraints))


def _schema_requires_migration(conn: sqlite3.Connection, tables: set[str]) -> bool:
    for table, expected_columns in CURRENT_SCHEMA_COLUMNS.items():
        if table not in tables or not expected_columns.issubset(_table_columns(conn, table)):
            return True
    for table, expected_primary_key in CURRENT_PRIMARY_KEYS.items():
        if table not in tables or tuple(_primary_key_columns(conn, table)) != expected_primary_key:
            return True
    for table, expected_constraints in CURRENT_FOREIGN_KEY_CONSTRAINTS.items():
        if table not in tables or _foreign_key_constraints(conn, table) != tuple(
            sorted(expected_constraints)
        ):
            return True
    return False


def _preserved_counts(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    return {
        table: int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_sqlite_identifier(table)}"
            ).fetchone()[0]
        )
        for table in MIGRATION_PRESERVED_TABLES
        if table in tables
    }


def _log_sequences(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int | None]:
    if "sqlite_sequence" not in tables:
        return {table: None for table in CURRENT_LOG_FOREIGN_KEYS}
    result: dict[str, int | None] = {}
    for table in CURRENT_LOG_FOREIGN_KEYS:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            (table,),
        ).fetchone()
        result[table] = int(row[0]) if row is not None else None
    return result


def _run_schema_migration(database_path: Path) -> None:
    try:
        db_module.init_schema_at(database_path)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise RuntimeError(f"备份 schema 迁移失败：{exc}") from exc


def _rehearse_backup_migration(source_path: Path) -> dict:
    report = {
        "attempted": True,
        "ok": False,
        "source_counts": {},
        "migrated_counts": {},
        "source_sequences": {},
        "migrated_sequences": {},
        "integrity_check": None,
        "foreign_key_check": [],
        "schema_current": False,
        "errors": [],
    }
    try:
        source = _open_sqlite_readonly(source_path)
        try:
            source_tables = _table_names(source)
            report["source_counts"] = _preserved_counts(source, source_tables)
            report["source_sequences"] = _log_sequences(source, source_tables)
        finally:
            source.close()

        with tempfile.TemporaryDirectory(prefix="douyin-recall-backup-validate-") as tmp:
            migrated_path = Path(tmp) / "recall.db"
            _sqlite_backup_snapshot(source_path, migrated_path)
            _run_schema_migration(migrated_path)
            migrated = _open_sqlite_readonly(migrated_path)
            try:
                integrity = migrated.execute("PRAGMA integrity_check").fetchone()
                report["integrity_check"] = integrity[0] if integrity else None
                migrated_tables = _table_names(migrated)
                all_migrated_counts = _preserved_counts(migrated, migrated_tables)
                report["migrated_counts"] = {
                    table: all_migrated_counts[table]
                    for table in report["source_counts"]
                    if table in all_migrated_counts
                }
                report["migrated_sequences"] = _log_sequences(migrated, migrated_tables)
                report["missing_required_tables"] = sorted(
                    REQUIRED_RESTORE_TABLES - migrated_tables
                )
                report["schema_current"] = not _schema_requires_migration(
                    migrated,
                    migrated_tables,
                )
                report["foreign_key_check"] = [
                    {
                        "table": row[0],
                        "rowid": row[1],
                        "parent": row[2],
                        "foreign_key_id": row[3],
                    }
                    for row in migrated.execute("PRAGMA foreign_key_check").fetchall()
                ]
            finally:
                migrated.close()

        if report["integrity_check"] != "ok":
            report["errors"].append(
                f"迁移副本 integrity_check 失败：{report['integrity_check']}"
            )
        if report["foreign_key_check"]:
            report["errors"].append(
                f"迁移副本仍有 {len(report['foreign_key_check'])} 条外键违规。"
            )
        if report.get("missing_required_tables"):
            report["errors"].append(
                "迁移副本仍缺少必要表：" + ", ".join(report["missing_required_tables"])
            )
        if not report["schema_current"]:
            report["errors"].append("迁移副本仍缺少当前版本要求的列、主键或外键。")
        if report["migrated_counts"] != report["source_counts"]:
            report["errors"].append("迁移副本关键表行数与原备份不一致。")
        for table, source_sequence in report["source_sequences"].items():
            migrated_sequence = report["migrated_sequences"].get(table)
            if source_sequence is not None and (
                migrated_sequence is None or migrated_sequence < source_sequence
            ):
                report["errors"].append(
                    f"迁移副本 {table} 的 sqlite_sequence 发生倒退。"
                )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        report["errors"].append(str(exc))
    report["ok"] = not report["errors"]
    return report


def validate_sqlite_backup(backup_path: Path | str) -> dict:
    path = Path(backup_path)
    report = {
        "ok": False,
        "path": str(path),
        "name": path.name,
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
        "integrity_check": None,
        "foreign_key_check": [],
        "schema_migration_required": False,
        "migration_validation": None,
        "required_tables_present": False,
        "missing_tables": [],
        "counts": {"favorites": 0, "likes": 0, "users": 0},
        "sidecars": {
            suffix: _sidecar_info(path, suffix)
            for suffix in ("-wal", "-shm", "-journal")
        },
        "warnings": [],
        "errors": [],
    }
    if not path.exists() or not path.is_file():
        report["errors"].append("备份文件不存在。")
        return report

    conn: sqlite3.Connection | None = None
    migration_candidate = False
    try:
        conn = _open_sqlite_readonly(path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        report["integrity_check"] = integrity[0] if integrity else None
        if report["integrity_check"] != "ok":
            report["errors"].append(f"SQLite integrity_check 失败：{report['integrity_check']}")

        tables = _table_names(conn)
        missing = sorted(REQUIRED_RESTORE_TABLES - tables)
        report["missing_tables"] = missing
        report["required_tables_present"] = not missing
        if missing:
            core_missing = sorted({"favorites", "likes"} - tables)
            if core_missing:
                report["errors"].append("缺少核心内容表：" + ", ".join(core_missing))
            else:
                migration_candidate = True

        try:
            report["foreign_key_check"] = [
                {
                    "table": row[0],
                    "rowid": row[1],
                    "parent": row[2],
                    "foreign_key_id": row[3],
                }
                for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            ]
        except sqlite3.Error as e:
            report["foreign_key_check_error"] = str(e)
            if "foreign key mismatch" in str(e).lower():
                migration_candidate = True
            else:
                report["errors"].append(f"SQLite foreign_key_check 失败：{e}")
        if report["foreign_key_check"]:
            report["errors"].append(
                f"SQLite foreign_key_check 发现 {len(report['foreign_key_check'])} 条违规。"
            )

        if not missing:
            migration_candidate = migration_candidate or _schema_requires_migration(conn, tables)

        for table in ("favorites", "likes", "users"):
            if table in tables:
                report["counts"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error as e:
        report["errors"].append(f"不是可读取的 SQLite 备份：{e}")
    finally:
        if conn is not None:
            conn.close()

    report["schema_migration_required"] = migration_candidate
    if migration_candidate and not report["errors"] and not report["foreign_key_check"]:
        migration_validation = _rehearse_backup_migration(path)
        report["migration_validation"] = migration_validation
        if migration_validation["ok"]:
            report["required_tables_present"] = True
            report["warnings"].append(
                "备份使用可迁移的旧数据库结构；恢复时会先迁移隔离副本。"
            )
        else:
            report["errors"].append(
                "旧数据库结构无法安全迁移：" + "；".join(migration_validation["errors"])
            )

    report["ok"] = not report["errors"]
    return report


def _file_sha256(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _copy_manifest_snapshot_locked(
    source_path: Path,
    destination_path: Path,
    expected_sha256: str,
) -> None:
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise ValueError("manifest restore requires an expected SHA256.")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(source_path), isolation_level=None, timeout=5)
    try:
        preexisting_sidecars = [
            suffix
            for suffix in ("-wal", "-journal")
            if int(_sidecar_info(source_path, suffix)["size_bytes"]) > 0
        ]
        if preexisting_sidecars:
            raise ValueError(
                "manifest source is not self-contained: " + ", ".join(preexisting_sidecars)
            )
        conn.execute("BEGIN IMMEDIATE")
        actual = (_file_sha256(source_path) or "").lower()
        if actual != expected:
            raise ValueError(
                f"manifest source SHA256 changed before restore: expected={expected} actual={actual}"
            )
        locked_sidecars = [
            suffix
            for suffix in ("-wal", "-journal")
            if int(_sidecar_info(source_path, suffix)["size_bytes"]) > 0
        ]
        if locked_sidecars:
            raise ValueError(
                "manifest source gained an unverified sidecar: " + ", ".join(locked_sidecars)
            )
        shutil.copy2(source_path, destination_path)
        copied = (_file_sha256(destination_path) or "").lower()
        if copied != expected:
            raise ValueError(
                f"manifest snapshot SHA256 mismatch: expected={expected} actual={copied}"
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _create_safety_backup(target_path: Path, safety_path: Path) -> tuple[bool, Path]:
    """Preserve the target and identify whether the copy is valid SQLite."""
    try:
        _sqlite_backup_snapshot(target_path, safety_path)
        validation = validate_sqlite_backup(safety_path)
        if validation["ok"]:
            return True, safety_path
    except sqlite3.Error:
        pass
    if safety_path.exists() and safety_path.is_file():
        safety_path.unlink()
    forensic_path = safety_path.with_suffix(".corrupt")
    shutil.copy2(target_path, forensic_path)
    for suffix in ("-wal", "-shm", "-journal"):
        source_sidecar = Path(str(target_path) + suffix)
        if source_sidecar.exists() and source_sidecar.is_file():
            shutil.copy2(source_sidecar, Path(str(forensic_path) + suffix))
    return False, forensic_path


def _restore_quarantined_sidecars(moved: list[tuple[Path, Path]]) -> None:
    errors: list[str] = []
    for original_path, quarantine_path in reversed(moved):
        if not quarantine_path.exists():
            continue
        try:
            os.replace(quarantine_path, original_path)
        except OSError as exc:
            errors.append(f"{quarantine_path} -> {original_path}: {exc}")
    if errors:
        raise RuntimeError("无法回滚数据库 sidecar 隔离：" + "；".join(errors))


def _quarantine_sqlite_sidecars(target_path: Path) -> list[tuple[Path, Path]]:
    """Move target sidecars away before the main-file replacement."""
    moved: list[tuple[Path, Path]] = []
    token = uuid.uuid4().hex
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(target_path) + suffix)
            if not sidecar.exists():
                continue
            if not sidecar.is_file():
                raise RuntimeError(f"数据库 sidecar 不是普通文件：{sidecar}")
            quarantine = target_path.parent / (
                f".{target_path.name}.restore-sidecar-{token}{suffix}"
            )
            os.replace(sidecar, quarantine)
            moved.append((sidecar, quarantine))
    except Exception as move_error:
        try:
            _restore_quarantined_sidecars(moved)
        except Exception as rollback_error:
            raise ExceptionGroup(
                "数据库 sidecar 隔离失败，且已移动文件也无法回滚。",
                [move_error, rollback_error],
            )
        raise
    return moved


def _cleanup_restore_stage(prepared_path: Path, warnings: list[str]) -> None:
    """Best-effort cleanup that never changes a committed restore into a failure."""
    for candidate in (
        prepared_path,
        *(Path(str(prepared_path) + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ):
        try:
            if not candidate.exists():
                continue
            if not candidate.is_file():
                raise OSError("path is not a regular file")
            candidate.unlink()
        except OSError as exc:
            warning = f"未能清理恢复临时文件 {candidate}: {exc}"
            warnings.append(warning)
            logger.warning(warning)


def _table_counts(path: Path, table_names: list[str]) -> tuple[dict[str, int], list[str]]:
    if not path.exists() or not path.is_file():
        return {}, list(table_names)
    conn = sqlite3.connect(str(path))
    try:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing = [table for table in table_names if table not in existing]
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {_sqlite_identifier(table)}").fetchone()[0])
            for table in table_names
            if table in existing
        }
        return counts, missing
    finally:
        conn.close()


def _manifest_backup_entry(manifest: dict) -> dict:
    evidence = manifest.get("evidence") or {}
    pre_release = evidence.get("pre_release_backup") or {}
    return pre_release.get("backup") or {}


def _manifest_path_value(path_text: str | None, manifest_path: Path) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return path
    return manifest_path.parent / path


def validate_delivery_manifest_backup(manifest_path: Path | str) -> dict:
    """Validate a delivery manifest's pre-release rollback backup."""
    path = Path(manifest_path)
    errors: list[str] = []
    backup_report = {
        "path": None,
        "expected_sha256": None,
        "actual_sha256": None,
        "expected_counts": {},
        "backup_counts": {},
        "validation": None,
    }

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"无法读取 delivery manifest：{exc}")
        manifest = {}
    except json.JSONDecodeError as exc:
        errors.append(f"delivery manifest 不是有效 JSON：{exc}")
        manifest = {}

    backup_entry = _manifest_backup_entry(manifest)
    backup_path = _manifest_path_value(backup_entry.get("path"), path)
    expected_sha = backup_entry.get("sha256")
    raw_counts = backup_entry.get("source_counts") or backup_entry.get("backup_counts") or {}
    try:
        if not isinstance(raw_counts, dict):
            raise TypeError("counts must be an object")
        expected_counts = {}
        for table, count in raw_counts.items():
            if isinstance(count, bool) or not isinstance(count, int):
                raise TypeError(f"{table} count must be an integer")
            if count < 0:
                raise ValueError(f"{table} count cannot be negative")
            expected_counts[str(table)] = count
    except (TypeError, ValueError) as exc:
        errors.append(f"delivery manifest 的关键表数量无效：{exc}")
        expected_counts = {}
    backup_report["expected_sha256"] = expected_sha
    backup_report["expected_counts"] = expected_counts
    if not str(expected_sha or "").strip():
        errors.append("delivery manifest 缺少 pre_release_backup.backup.sha256。")
    if not expected_counts:
        errors.append("delivery manifest 缺少有效的关键表数量。")
    else:
        missing_count_tables = sorted(MANIFEST_REQUIRED_COUNT_TABLES - set(expected_counts))
        if missing_count_tables:
            errors.append(
                "delivery manifest 缺少关键表数量：" + ", ".join(missing_count_tables)
            )

    if backup_path is None:
        errors.append("delivery manifest 缺少 pre_release_backup.backup.path。")
    else:
        backup_report["path"] = str(backup_path)
        actual_sha = _file_sha256(backup_path)
        backup_report["actual_sha256"] = actual_sha
        if not actual_sha:
            errors.append("manifest 指向的备份文件不存在或不可读。")
        elif expected_sha and actual_sha.lower() != str(expected_sha).lower():
            errors.append(f"SHA256 不匹配：manifest={expected_sha} actual={actual_sha}")

        validation = validate_sqlite_backup(backup_path)
        backup_report["validation"] = validation
        if not validation["ok"]:
            errors.extend(validation["errors"])
        nonempty_manifest_sidecars = [
            suffix
            for suffix in ("-wal", "-journal")
            if int((validation.get("sidecars", {}).get(suffix) or {}).get("size_bytes") or 0) > 0
        ]
        if nonempty_manifest_sidecars:
            errors.append(
                "manifest 备份必须是自包含 SQLite 文件；发现未纳入 SHA256 的 sidecar："
                + ", ".join(nonempty_manifest_sidecars)
            )

        if expected_counts:
            table_names = list(expected_counts)
            backup_counts, missing = _table_counts(backup_path, table_names)
            backup_report["backup_counts"] = backup_counts
            for table in missing:
                errors.append(f"备份缺少 manifest 记录的关键表：{table}")
            for table, expected in expected_counts.items():
                actual = backup_counts.get(table)
                if actual is not None and actual != expected:
                    errors.append(f"关键表数量不一致：{table} manifest={expected} backup={actual}")

    return {
        "ok": not errors,
        "manifest_path": str(path),
        "backup": backup_report,
        "errors": errors,
    }


def restore_from_delivery_manifest(
    manifest_path: Path | str,
    *,
    apply: bool = False,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    close_connection=None,
) -> dict:
    """Validate a delivery manifest rollback backup and optionally restore it."""
    validation = validate_delivery_manifest_backup(manifest_path)
    report = {
        "ok": bool(validation["ok"]),
        "mode": "apply" if apply else "dry_run",
        "restored": False,
        "validation": validation,
        "restore": None,
        "errors": list(validation["errors"]),
    }
    if not validation["ok"]:
        return report
    if not apply:
        return report

    try:
        result = restore_sqlite_backup(
            validation["backup"]["path"],
            db_path=db_path,
            backup_dir=backup_dir,
            close_connection=close_connection,
            expected_sha256=validation["backup"]["expected_sha256"],
            expected_counts=validation["backup"]["expected_counts"],
            require_self_contained=True,
        )
    except Exception as exc:
        report["ok"] = False
        report["errors"].append(str(exc))
        return report

    report["restored"] = True
    report["restore"] = {
        "backup_path": str(result.backup_path),
        "restored_path": str(result.restored_path),
        "safety_backup_path": (
            str(result.safety_backup_path) if result.safety_backup_path is not None else None
        ),
        "validation": result.validation,
        "cleanup_warnings": list(result.cleanup_warnings),
    }
    return report


def verify_latest_backup(output_dir: Path | None = None) -> dict:
    backups = list_recovery_backups(output_dir, limit=1)
    if not backups:
        return {
            "ok": False,
            "backup": None,
            "validation": None,
            "errors": ["没有找到可校验的备份文件。"],
        }

    backup = backups[0]
    validation = validate_sqlite_backup(backup.path)
    return {
        "ok": bool(validation["ok"]),
        "backup": backup.__dict__,
        "validation": validation,
        "errors": list(validation["errors"]),
    }


def restore_sqlite_backup(
    backup_path: Path | str,
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    close_connection=None,
    expected_sha256: str | None = None,
    expected_counts: dict[str, int] | None = None,
    require_self_contained: bool = False,
) -> RestoreResult:
    source_path = Path(backup_path)
    target_path = Path(db_path) if db_path is not None else Path(settings.db_path)
    safety_dir = _backup_dir(backup_dir)
    validation = validate_sqlite_backup(source_path)
    if not validation["ok"]:
        raise ValueError("备份校验未通过：" + "；".join(validation["errors"]))
    try:
        if source_path.resolve() == target_path.resolve():
            raise ValueError("不能从当前正在使用的数据库文件恢复。")
    except FileNotFoundError:
        pass

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        prefix=f".{target_path.name}.restore-",
        suffix=".db",
        dir=str(target_path.parent),
        delete=False,
    )
    prepared_path = Path(temp_file.name)
    temp_file.close()
    safety_path: Path | None = None
    cleanup_warnings: list[str] = []
    try:
        if require_self_contained:
            _copy_manifest_snapshot_locked(
                source_path,
                prepared_path,
                str(expected_sha256 or ""),
            )
        else:
            _sqlite_backup_snapshot(source_path, prepared_path)
        _run_schema_migration(prepared_path)
        prepared_validation = validate_sqlite_backup(prepared_path)
        if not prepared_validation["ok"] or prepared_validation["schema_migration_required"]:
            errors = list(prepared_validation["errors"])
            if prepared_validation["schema_migration_required"]:
                errors.append("恢复临时副本在迁移后仍不是当前 schema。")
            raise ValueError("恢复临时副本校验未通过：" + "；".join(errors))
        if expected_counts:
            migrated_counts, missing = _table_counts(
                prepared_path,
                list(expected_counts),
            )
            count_errors = [
                f"{table}: expected={expected} actual={migrated_counts.get(table)}"
                for table, expected in expected_counts.items()
                if table in missing or migrated_counts.get(table) != int(expected)
            ]
            if count_errors:
                raise ValueError(
                    "恢复临时副本与 manifest 关键表数量不一致：" + "；".join(count_errors)
                )

        with db_module.block_new_connections():
            if close_connection is not None:
                close_connection()
            target_was_valid_sqlite = True
            target_existed = target_path.exists()
            if target_existed:
                safety_dir.mkdir(parents=True, exist_ok=True)
                safety_path = safety_dir / (
                    "pre-restore-recall-"
                    + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f-")
                    + uuid.uuid4().hex[:8]
                    + ".db"
                )
                target_was_valid_sqlite, safety_path = _create_safety_backup(
                    target_path,
                    safety_path,
                )
                if target_was_valid_sqlite:
                    _consolidate_sqlite_file(target_path)
            quarantined_sidecars = _quarantine_sqlite_sidecars(target_path)
            try:
                os.replace(prepared_path, target_path)
            except Exception as replace_error:
                try:
                    _restore_quarantined_sidecars(quarantined_sidecars)
                except Exception as rollback_error:
                    safety_hint = str(safety_path) if safety_path is not None else "未创建"
                    raise ExceptionGroup(
                        "数据库主文件替换失败，sidecar 回滚也失败；"
                        f"请保留现场并使用安全备份恢复：{safety_hint}",
                        [replace_error, rollback_error],
                    )
                raise
            for _original_path, quarantine_path in quarantined_sidecars:
                try:
                    quarantine_path.unlink()
                except OSError as exc:
                    warning = f"恢复已完成，但未能清理隔离 sidecar {quarantine_path}: {exc}"
                    cleanup_warnings.append(warning)
                    logger.warning(warning)
    finally:
        _cleanup_restore_stage(prepared_path, cleanup_warnings)

    return RestoreResult(
        backup_path=source_path,
        restored_path=target_path,
        safety_backup_path=safety_path,
        validation=validation,
        cleanup_warnings=tuple(cleanup_warnings),
    )

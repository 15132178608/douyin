"""Maintenance status aggregation and long-running local upkeep helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any

from src import jobs
from src import server_runtime
from src import update_check
from src.config import PROJECT_ROOT
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
ORDINARY_BACKUP_PATTERN = "recall-backup-*.db"
PROTECTED_BACKUP_PATTERNS = (
    "pre-install-recall-*.db",
    "pre-restore-recall-*.db",
    "pre-release-recall-*.db",
)
RECOVERY_BACKUP_PATTERNS = (ORDINARY_BACKUP_PATTERN, *PROTECTED_BACKUP_PATTERNS)
BACKUP_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})")
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
    safety_backup_path: Path
    validation: dict


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


def validate_sqlite_backup(backup_path: Path | str) -> dict:
    path = Path(backup_path)
    report = {
        "ok": False,
        "path": str(path),
        "name": path.name,
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
        "integrity_check": None,
        "required_tables_present": False,
        "missing_tables": [],
        "counts": {"favorites": 0, "likes": 0, "users": 0},
        "errors": [],
    }
    if not path.exists() or not path.is_file():
        report["errors"].append("备份文件不存在。")
        return report

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        report["integrity_check"] = integrity[0] if integrity else None
        if report["integrity_check"] != "ok":
            report["errors"].append(f"SQLite integrity_check 失败：{report['integrity_check']}")

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_RESTORE_TABLES - tables)
        report["missing_tables"] = missing
        report["required_tables_present"] = not missing
        if missing:
            report["errors"].append("缺少必要表：" + ", ".join(missing))

        for table in ("favorites", "likes", "users"):
            if table in tables:
                report["counts"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error as e:
        report["errors"].append(f"不是可读取的 SQLite 备份：{e}")
    finally:
        if conn is not None:
            conn.close()

    report["ok"] = not report["errors"]
    return report


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


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
    expected_counts = {
        str(table): int(count)
        for table, count in (backup_entry.get("source_counts") or backup_entry.get("backup_counts") or {}).items()
    }
    backup_report["expected_sha256"] = expected_sha
    backup_report["expected_counts"] = expected_counts

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
        )
    except Exception as exc:
        report["ok"] = False
        report["errors"].append(str(exc))
        return report

    report["restored"] = True
    report["restore"] = {
        "backup_path": str(result.backup_path),
        "restored_path": str(result.restored_path),
        "safety_backup_path": str(result.safety_backup_path),
        "validation": result.validation,
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
) -> RestoreResult:
    source_path = Path(backup_path)
    target_path = Path(db_path) if db_path is not None else PROJECT_ROOT / "data" / "recall.db"
    safety_dir = _backup_dir(backup_dir)
    validation = validate_sqlite_backup(source_path)
    if not validation["ok"]:
        raise ValueError("备份校验未通过：" + "；".join(validation["errors"]))
    try:
        if source_path.resolve() == target_path.resolve():
            raise ValueError("不能从当前正在使用的数据库文件恢复。")
    except FileNotFoundError:
        pass

    safety_dir.mkdir(parents=True, exist_ok=True)
    safety_path = safety_dir / f"pre-restore-recall-{_timestamp()}.db"
    if target_path.exists():
        current = sqlite3.connect(str(target_path))
        safety = sqlite3.connect(str(safety_path))
        try:
            current.backup(safety)
        finally:
            safety.close()
            current.close()
    else:
        safety_path.write_bytes(b"")

    if close_connection is not None:
        close_connection()

    shutil.copy2(source_path, target_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target_path) + suffix)
        if sidecar.exists() and sidecar.is_file():
            sidecar.unlink()

    return RestoreResult(
        backup_path=source_path,
        restored_path=target_path,
        safety_backup_path=safety_path,
        validation=validation,
    )

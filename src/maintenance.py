"""Maintenance status aggregation and long-running local upkeep helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
REQUIRED_RESTORE_TABLES = {
    "users",
    "favorites",
    "likes",
    "job_queue",
    "crawl_runs",
    "like_crawl_runs",
}
RECOVERY_BACKUP_PATTERNS = ("recall-backup-*.db", "pre-install-recall-*.db")
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
    def sort_key(path: Path) -> tuple[str, float, str]:
        timestamp = BACKUP_TIMESTAMP_RE.search(path.name)
        return (
            timestamp.group(1) if timestamp else "",
            path.stat().st_mtime,
            path.name,
        )

    files.sort(key=sort_key, reverse=True)
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


def _douyin_auth_recovery_summary(user_id: str, crawl_runs: dict[str, dict]) -> dict:
    uid = normalize_user_id(user_id)
    errors: list[dict] = []

    for job in jobs.list_jobs(user_id=uid, limit=50):
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
    if user_id == DEFAULT_USER_ID:
        indexed = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {kind.vector_table} WHERE id LIKE ? OR id NOT LIKE '%:%'",
                (f"{user_id}:%",),
            ).fetchone()[0]
        )
    else:
        indexed = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {kind.vector_table} WHERE id LIKE ?",
                (f"{user_id}:%",),
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


def _job_summary(user_id: str) -> dict:
    summary = {"pending": 0, "running": 0, "failed": 0, "success": 0, "total": 0}
    for job in jobs.list_jobs(user_id=user_id, limit=200):
        summary["total"] += 1
        status = job.get("status")
        if status in summary:
            summary[status] += 1
    summary["needs_attention"] = summary["failed"] > 0 or summary["running"] > 0
    return summary


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
    contents = {kind.key: _content_summary(uid, kind.key) for kind in list_content_kinds()}
    crawl_runs = {kind.key: _crawl_run_summary(uid, kind.key) for kind in list_content_kinds()}
    job_summary = _job_summary(uid)
    auth_summary = _douyin_auth_recovery_summary(uid, crawl_runs)

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

    return {
        "user_id": uid,
        "contents": contents,
        "favorites": contents["favorites"],
        "likes": contents["likes"],
        "crawl_runs": crawl_runs,
        "jobs": job_summary,
        "auth": auth_summary,
        "server": server_runtime.get_server_status(),
        "backups": {
            "output_dir": str(backup_root),
            "count": len(backups),
            "latest": backups[0].__dict__ if backups else None,
            "items": [item.__dict__ for item in backups],
        },
        "update": update_status,
        "attention_codes": attention_codes,
        "needs_attention": bool(attention_codes),
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

    return exporter.backup_sqlite(_backup_dir(output_dir))


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

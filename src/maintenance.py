"""Maintenance status aggregation and long-running local upkeep helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from src import jobs
from src import server_runtime
from src.config import PROJECT_ROOT
from src.content.kinds import get_content_kind, list_content_kinds
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "exports"
REQUIRED_RESTORE_TABLES = {
    "users",
    "favorites",
    "likes",
    "job_queue",
    "crawl_runs",
    "like_crawl_runs",
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


def _sqlite_row_to_dict(row: Any | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


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
) -> dict:
    uid = normalize_user_id(user_id)
    backups = list_sqlite_backups(backup_dir)
    backup_root = _backup_dir(backup_dir)
    contents = {kind.key: _content_summary(uid, kind.key) for kind in list_content_kinds()}
    crawl_runs = {kind.key: _crawl_run_summary(uid, kind.key) for kind in list_content_kinds()}
    job_summary = _job_summary(uid)

    attention_codes: list[str] = []
    if job_summary["failed"] > 0:
        attention_codes.append("failed_jobs")
    if not backups:
        attention_codes.append("no_backups")
    for key, run in crawl_runs.items():
        latest = run.get("latest") or {}
        if latest.get("status") == "failed":
            attention_codes.append(f"latest_{key}_crawl_failed")
    for key, summary in contents.items():
        if summary["needs_index"]:
            attention_codes.append(f"{key}_needs_index")

    return {
        "user_id": uid,
        "contents": contents,
        "favorites": contents["favorites"],
        "likes": contents["likes"],
        "crawl_runs": crawl_runs,
        "jobs": job_summary,
        "server": server_runtime.get_server_status(),
        "backups": {
            "output_dir": str(backup_root),
            "count": len(backups),
            "latest": backups[0].__dict__ if backups else None,
            "items": [item.__dict__ for item in backups],
        },
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

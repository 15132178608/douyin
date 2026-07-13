"""Structured local environment diagnostics for CLI, scripts, and UI reuse."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from src import jobs
from src import maintenance
from src import server_runtime
from src.config import PROJECT_ROOT, settings


def _check(status: str, ok: bool, message: str, details: dict[str, Any] | None = None) -> dict:
    return {
        "status": status,
        "ok": bool(ok),
        "message": message,
        "details": details or {},
    }


def _command_version(command: str, *args: str) -> dict:
    path = shutil.which(command)
    if not path:
        return _check("missing", False, f"未找到 {command}。", {"command": command})
    try:
        result = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive OS boundary
        return _check("error", False, f"{command} 可执行文件存在，但无法运行。", {"path": path, "error": str(exc)})
    text = (result.stdout or result.stderr or "").strip()
    return _check(
        "ok" if result.returncode == 0 else "error",
        result.returncode == 0,
        f"{command} 可用。" if result.returncode == 0 else f"{command} 运行失败。",
        {"path": path, "version": text, "exit_code": result.returncode},
    )


def _module_check(module_name: str, label: str | None = None) -> dict:
    found = importlib.util.find_spec(module_name) is not None
    display = label or module_name
    return _check(
        "ok" if found else "missing",
        found,
        f"{display} 已安装。" if found else f"缺少 {display}。",
        {"module": module_name},
    )


def _database_check(db_path: Path | None = None) -> dict:
    path = Path(db_path or settings.db_path)
    details: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "required_tables": sorted(maintenance.REQUIRED_RESTORE_TABLES),
        "missing_tables": [],
    }
    if not path.exists():
        return _check("missing", False, "数据库文件不存在，请先初始化或完成首次设置。", details)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        details["integrity_check"] = integrity[0] if integrity else None
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing = sorted(maintenance.REQUIRED_RESTORE_TABLES - tables)
        details["missing_tables"] = missing
        details["table_count"] = len(tables)
        if details["integrity_check"] != "ok":
            return _check("error", False, "数据库完整性检查失败。", details)
        if missing:
            return _check("error", False, "数据库缺少必要表。", details)
        return _check("ok", True, "数据库可读取，必要表完整。", details)
    except sqlite3.Error as exc:
        details["error"] = str(exc)
        return _check("error", False, "数据库无法读取。", details)
    finally:
        if conn is not None:
            conn.close()


def _playwright_chromium_check() -> dict:
    candidates: list[str] = []
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        candidates.append(str(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])))
    candidates.extend(
        [
            str(PROJECT_ROOT / ".venv"),
            str(Path.home() / "AppData" / "Local" / "ms-playwright"),
        ]
    )
    found = any(Path(path).exists() and any(Path(path).glob("chromium*")) for path in candidates)
    return _check(
        "ok" if found else "warning",
        True,
        "已发现 Playwright Chromium 缓存。" if found else "未确认 Chromium 缓存；首次运行可能需要安装。",
        {"candidate_roots": candidates, "found": found},
    )


def _backups_check(backup_dir: Path | None = None) -> dict:
    root = maintenance._backup_dir(backup_dir)  # Reuse the central path policy.
    retention = maintenance.describe_backup_retention(root)
    backups = maintenance.list_recovery_backups(root, limit=8)
    return _check(
        "ok" if backups else "warning",
        True,
        "已找到 SQLite 备份。" if backups else "还没有 SQLite 备份。",
        {
            "output_dir": str(root),
            "count": len(backups),
            "latest": backups[0].__dict__ if backups else None,
            "retention": retention,
        },
    )


def _jobs_check() -> dict:
    try:
        rows = jobs.list_jobs(limit=200)
    except Exception as exc:
        return _check("error", False, "后台任务队列无法读取。", {"error": str(exc)})
    summary = {"pending": 0, "running": 0, "failed": 0, "success": 0}
    for row in rows:
        status = row.get("status")
        if status in summary:
            summary[status] += 1
    return _check("ok", True, "后台任务队列可读取。", {"count": len(rows), "summary": summary})


def _path_check(path: Path, label: str, *, create_expected: bool = False) -> dict:
    exists = path.exists()
    status = "ok" if exists else "warning"
    message = f"{label}目录存在。" if exists else f"{label}目录不存在，运行时会创建。"
    return _check(status, True, message, {"path": str(path), "exists": exists, "create_expected": create_expected})


def _smtp_check() -> dict:
    configured = bool(settings.smtp_host and settings.mail_to)
    return _check(
        "ok" if configured else "warning",
        True,
        "SMTP 已配置。" if configured else "SMTP 未完整配置；邮件 digest 会不可用。",
        {
            "smtp_host_set": bool(settings.smtp_host),
            "smtp_user_set": bool(settings.smtp_user),
            "mail_to_set": bool(settings.mail_to),
        },
    )


def collect_doctor_report(*, backup_dir: Path | None = None, db_path: Path | None = None) -> dict:
    """Return a stable diagnostic report suitable for JSON output and UI rendering."""
    checks = {
        "python": _check(
            "ok",
            True,
            "Python 可用。",
            {"executable": sys.executable, "version": sys.version.split()[0]},
        ),
        "uv": _command_version("uv", "--version"),
        "playwright": _module_check("playwright", "Playwright"),
        "chromium": _playwright_chromium_check(),
        "sqlite": _check(
            "ok",
            True,
            "SQLite 可用。",
            {"sqlite_version": sqlite3.sqlite_version},
        ),
        "database": _database_check(db_path),
        "backups": _backups_check(backup_dir),
        "jobs": _jobs_check(),
        "web_service": _check(
            "ok",
            True,
            "本地 Web 服务状态已读取。",
            server_runtime.get_service_audit(configured_port=settings.web_port),
        ),
        "model_cache": _path_check(PROJECT_ROOT / "data" / "models", "模型缓存", create_expected=True),
        "avatar_cache": _path_check(settings.avatar_cache_dir, "头像缓存", create_expected=True),
        "smtp": _smtp_check(),
    }
    fatal = [name for name, check in checks.items() if check["status"] == "error" or check["ok"] is False]
    return {
        "ok": not fatal,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "checks": checks,
        "fatal_checks": fatal,
    }


def render_doctor_text(report: dict) -> str:
    lines = [
        "环境诊断",
        f"- 项目目录: {report['project_root']}",
        f"- 检查时间: {report['checked_at']}",
        "",
    ]
    labels = {
        "python": "Python",
        "uv": "uv",
        "playwright": "Playwright",
        "chromium": "Chromium",
        "sqlite": "SQLite",
        "database": "数据库",
        "backups": "备份",
        "jobs": "后台任务",
        "web_service": "Web 服务",
        "model_cache": "模型缓存",
        "avatar_cache": "头像缓存",
        "smtp": "SMTP",
    }
    for key, item in report["checks"].items():
        label = labels.get(key, key)
        lines.append(f"- {label}: {item['message']} ({item['status']})")
    if report.get("fatal_checks"):
        lines.extend(["", "需要先处理失败项后再发布或继续排查。"])
    else:
        lines.extend(["", "没有发现阻断性问题。"])
    return "\n".join(lines)

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


WINDOWS_RUNTIME_MODEL_CACHE = Path(
    r"D:\codexDownload\douyinclaude-runtime\huggingface"
)
MODEL_CACHE_ENV_VARS = (
    "SENTENCE_TRANSFORMERS_HOME",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
)
MODEL_CACHE_SNAPSHOT_PATTERNS = (
    "models--*/snapshots/*",
    "hub/models--*/snapshots/*",
    "sentence-transformers/models--*/snapshots/*",
)
MODEL_CACHE_MARKERS = (
    "config.json",
    "modules.json",
    "model.safetensors",
    "pytorch_model.bin",
)
MODEL_CACHE_METADATA_NAMES = {
    ".ds_store",
    ".gitattributes",
    ".gitkeep",
    "cachedir.tag",
    "desktop.ini",
    "thumbs.db",
}
MODEL_CACHE_IGNORED_DIRECTORY_NAMES = {
    ".locks",
    ".no_exist",
    "blobs",
    "refs",
    "xet",
}
MODEL_CACHE_PARTIAL_SUFFIXES = (".incomplete", ".lock", ".part", ".tmp")
MODEL_CACHE_PAYLOAD_NAMES = {
    "config.json",
    "flax_model.msgpack",
    "merges.txt",
    "model.safetensors",
    "modules.json",
    "pytorch_model.bin",
    "rust_model.ot",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "spiece.model",
    "tf_model.h5",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "vocab.txt",
}
MODEL_CACHE_PAYLOAD_SUFFIXES = (
    ".bin",
    ".h5",
    ".msgpack",
    ".onnx",
    ".ot",
    ".pt",
    ".pth",
    ".safetensors",
)
MODEL_CACHE_MAX_SCAN_DEPTH = 12
MODEL_CACHE_MAX_SCAN_DIRECTORIES = 512
MODEL_CACHE_MAX_SCAN_FILES = 4096


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
        "foreign_key_check": [],
    }
    if not path.exists():
        return _check("missing", False, "数据库文件不存在，请先初始化或完成首次设置。", details)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
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
        try:
            details["foreign_key_check"] = [
                {
                    "table": row[0],
                    "rowid": row[1],
                    "parent": row[2],
                    "foreign_key_id": row[3],
                }
                for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            ]
        except sqlite3.Error as exc:
            details["foreign_key_check_error"] = str(exc)
            return _check("error", False, "数据库外键结构检查失败。", details)
        if details["foreign_key_check"]:
            return _check("error", False, "数据库存在外键约束违规。", details)
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


def _model_cache_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    for variable in MODEL_CACHE_ENV_VARS:
        value = os.environ.get(variable)
        if value and value.strip():
            candidates.append((variable, Path(value).expanduser()))
    if os.name == "nt":
        candidates.append(("windows_runtime_default", WINDOWS_RUNTIME_MODEL_CACHE))
    candidates.append(("project_data_models", PROJECT_ROOT / "data" / "models"))

    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source, path in candidates:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, path))
    return unique


def _is_model_payload_file(
    path: Path,
    *,
    candidate_root: Path | None = None,
    require_model_signature: bool = False,
) -> bool:
    name = path.name.lower()
    if name in MODEL_CACHE_METADATA_NAMES or name.endswith(MODEL_CACHE_PARTIAL_SUFFIXES):
        return False
    try:
        relative_parts = path.relative_to(candidate_root).parts if candidate_root is not None else path.parts
    except ValueError:
        return False
    if any(part.lower() in MODEL_CACHE_IGNORED_DIRECTORY_NAMES for part in relative_parts[:-1]):
        return False
    if (
        require_model_signature
        and name not in MODEL_CACHE_PAYLOAD_NAMES
        and not name.endswith(MODEL_CACHE_PAYLOAD_SUFFIXES)
    ):
        return False
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as handle:
            return bool(handle.read(1))
    except (OSError, ValueError):
        return False


def _find_model_payload(root: Path, *, require_model_signature: bool = False) -> Path | None:
    scanned_directories = 0
    scanned_files = 0
    try:
        for current_value, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            scanned_directories += 1
            if scanned_directories > MODEL_CACHE_MAX_SCAN_DIRECTORIES:
                return None

            current = Path(current_value)
            try:
                depth = len(current.relative_to(root).parts)
            except ValueError:
                return None

            directory_names[:] = [
                name
                for name in sorted(directory_names)
                if name.lower() not in MODEL_CACHE_IGNORED_DIRECTORY_NAMES
                and not name.lower().endswith(MODEL_CACHE_PARTIAL_SUFFIXES)
            ]
            if depth >= MODEL_CACHE_MAX_SCAN_DEPTH:
                directory_names[:] = []

            for name in sorted(file_names):
                scanned_files += 1
                if scanned_files > MODEL_CACHE_MAX_SCAN_FILES:
                    return None
                candidate = current / name
                if _is_model_payload_file(
                    candidate,
                    candidate_root=root,
                    require_model_signature=require_model_signature,
                ):
                    return candidate
    except OSError:
        return None
    return None


def _model_cache_evidence(root: Path, *, allow_legacy_payload: bool = False) -> Path | None:
    try:
        if not root.is_dir():
            return None
        for pattern in MODEL_CACHE_SNAPSHOT_PATTERNS:
            for snapshot in root.glob(pattern):
                if snapshot.is_dir():
                    evidence = _find_model_payload(snapshot, require_model_signature=True)
                    if evidence is not None:
                        return evidence
        for marker in MODEL_CACHE_MARKERS:
            direct = root / marker
            if _is_model_payload_file(direct, candidate_root=root):
                return direct
            for nested in root.glob(f"*/{marker}"):
                if _is_model_payload_file(nested, candidate_root=root):
                    return nested
        if allow_legacy_payload:
            return _find_model_payload(root)
    except OSError:
        return None
    return None


def _model_cache_check() -> dict:
    candidates = _model_cache_candidates()
    existing_roots: list[str] = []
    detected_source: str | None = None
    detected_root: Path | None = None
    evidence: Path | None = None
    for source, root in candidates:
        if root.is_dir():
            existing_roots.append(str(root))
        evidence = _model_cache_evidence(
            root,
            allow_legacy_payload=source == "project_data_models",
        )
        if evidence is not None:
            detected_source = source
            detected_root = root
            break

    found = evidence is not None
    return _check(
        "ok" if found else "warning",
        True,
        "已发现本地模型缓存。" if found else "未确认本地模型缓存；首次搜索可能需要下载模型。",
        {
            "candidate_roots": [str(path) for _source, path in candidates],
            "existing_roots": existing_roots,
            "found": found,
            "detected_source": detected_source,
            "detected_root": str(detected_root) if detected_root else None,
            "evidence_path": str(evidence) if evidence else None,
        },
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
        "model_cache": _model_cache_check(),
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

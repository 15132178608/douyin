"""Create privacy-preserving diagnostic bundles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import platform
from pathlib import Path
import re
import sys
import zipfile

from src.config import PROJECT_ROOT, settings
from src import server_runtime


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "diagnostics"
DEFAULT_LOGS_DIR = PROJECT_ROOT / "data" / "logs"
DEPENDENCY_MODULES = (
    "sqlite_vec",
    "playwright",
    "sentence_transformers",
    "jieba",
    "fastapi",
    "loguru",
    "pydantic_settings",
)
EXCLUDED_SENSITIVE_PATHS = (
    ".env",
    "data/recall.db",
    "data/recall.db-wal",
    "data/recall.db-shm",
    "data/playwright_profile",
    "data/chrome_cdp_profile",
    "data/users",
    "data/auth",
    ".venv",
)


@dataclass(frozen=True)
class DiagnosticBundleResult:
    path: Path
    file_count: int


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def redact_text(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<redacted-email>", text)
    text = re.sub(
        r"\b(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+",
        lambda m: f"{m.group(1)}: <redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)\s*=\s*[^\s]+",
        lambda m: f"{m.group(1)}=<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "sk-<redacted>", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "github_pat_<redacted>", text)
    text = re.sub(r"ghp_[A-Za-z0-9_]{20,}", "ghp_<redacted>", text)
    return text


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _dependency_status() -> dict:
    return {
        name: {"available": importlib.util.find_spec(name) is not None}
        for name in DEPENDENCY_MODULES
    }


def _environment_summary(project_root: Path, logs_dir: Path) -> dict:
    db_path = project_root / "data" / "recall.db"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "project": {
            "root": str(project_root),
            "logs_dir_exists": logs_dir.exists(),
        },
        "config": {
            "web_host": settings.web_host,
            "web_port": settings.web_port,
            "web_auth_required": settings.web_auth_required,
            "smtp_configured": bool(settings.smtp_host and settings.smtp_user and settings.smtp_password),
            "mail_to_configured": bool(settings.mail_to),
        },
        "data": {
            "database_exists": db_path.exists(),
            "database_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "playwright_profile_exists": (project_root / "data" / "playwright_profile").exists(),
            "avatar_cache_exists": (project_root / "data" / "avatar_cache").exists(),
        },
        "dependencies": _dependency_status(),
    }


def _read_log_tail(path: Path, *, max_bytes: int) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return redact_text(data.decode("utf-8", errors="replace"))


def _write_recent_logs(zf: zipfile.ZipFile, logs_dir: Path, *, max_log_bytes: int) -> None:
    if not logs_dir.exists():
        return
    log_files = [
        p for p in logs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".log", ".out", ".err", ".txt"}
    ]
    log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in log_files[:8]:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name)
        zf.writestr(f"logs/{safe_name}.txt", _read_log_tail(path, max_bytes=max_log_bytes))


def create_diagnostic_bundle(
    output_dir: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    logs_dir: Path | None = None,
    service_status: dict | None = None,
    maintenance_status: dict | None = None,
    max_log_bytes: int = 200_000,
) -> DiagnosticBundleResult:
    project_root = Path(project_root)
    logs_dir = Path(logs_dir) if logs_dir is not None else project_root / "data" / "logs"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"douyin-recall-diagnostics-{_timestamp()}.zip"

    active_service_status = service_status if service_status is not None else server_runtime.get_server_status()
    if maintenance_status is None:
        try:
            from src import maintenance

            maintenance_status = maintenance.get_maintenance_status()
        except Exception as e:
            maintenance_status = {"error": redact_text(str(e))}

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": "douyin-recall-diagnostics-v1",
        "excluded_sensitive_paths": list(EXCLUDED_SENSITIVE_PATHS),
        "notes": [
            "This bundle intentionally excludes .env, SQLite databases, browser profiles, login state, and data/users.",
            "Recent logs are redacted with best-effort token, password, and email masking.",
        ],
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json_bytes(manifest))
        zf.writestr("environment.json", _json_bytes(_environment_summary(project_root, logs_dir)))
        zf.writestr("service_status.json", _json_bytes(active_service_status))
        zf.writestr("maintenance_status.json", _json_bytes(maintenance_status or {}))
        _write_recent_logs(zf, logs_dir, max_log_bytes=max(1, int(max_log_bytes or 1)))
        file_count = len(zf.namelist())

    return DiagnosticBundleResult(path=path, file_count=file_count)

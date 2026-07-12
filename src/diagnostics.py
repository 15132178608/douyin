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


def _safe_path_name(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return re.split(r"[\\/]+", text.rstrip("\\/"))[-1] or None


def _latest_file(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    files = [path for path in root.glob(pattern) if path.is_file()]
    if not files:
        return None
    files.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return files[0]


def _read_json_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


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


def _artifact_file_names(artifacts: dict) -> dict:
    return {
        str(label): file_name
        for label, value in artifacts.items()
        if (file_name := _safe_path_name(value))
    }


def _counts(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in payload.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _backup_summary(backup: object) -> dict | None:
    if not isinstance(backup, dict):
        return None
    validation = backup.get("validation") if isinstance(backup.get("validation"), dict) else {}
    summary = {
        "file": _safe_path_name(backup.get("path")),
        "sha256": backup.get("sha256") or backup.get("expected_sha256"),
        "actual_sha256": backup.get("actual_sha256"),
        "size_bytes": backup.get("size_bytes"),
        "source_counts": _counts(backup.get("source_counts") or backup.get("expected_counts")),
        "backup_counts": _counts(backup.get("backup_counts")),
        "validation_ok": validation.get("ok") if validation else None,
    }
    return {key: value for key, value in summary.items() if value not in (None, {}, "")}


def _rollback_summary(rollback: object) -> dict | None:
    if not isinstance(rollback, dict):
        return None
    validation = rollback.get("validation") if isinstance(rollback.get("validation"), dict) else {}
    summary = {
        "ok": bool(rollback.get("ok")),
        "mode": rollback.get("mode"),
        "restored": bool(rollback.get("restored")),
        "validation_ok": validation.get("ok") if validation else None,
    }
    rollback_backup = _backup_summary(validation.get("backup") if validation else None)
    if rollback_backup:
        summary["backup"] = rollback_backup
    return {key: value for key, value in summary.items() if value not in (None, {}, "")}


def _performance_summary(performance: object) -> dict | None:
    if not isinstance(performance, dict):
        return None
    regressions = performance.get("regressions")
    return {
        "baseline_status": performance.get("baseline_status"),
        "regressions_count": len(regressions) if isinstance(regressions, list) else 0,
    }


def _release_check_summary(check: object) -> dict:
    if not isinstance(check, dict):
        return {}
    return {
        "name": str(check.get("name") or ""),
        "ok": bool(check.get("ok")),
        "exit_code": int(check.get("exit_code", 1)),
        "elapsed_seconds": check.get("elapsed_seconds"),
    }


def _evidence_summary(evidence: object) -> dict:
    if not isinstance(evidence, dict):
        return {}
    summarized: dict[str, dict] = {}
    for name, item in sorted(evidence.items()):
        if not isinstance(item, dict):
            continue
        entry = {
            "ok": bool(item.get("ok")),
            "exit_code": int(item.get("exit_code", 1)),
            "elapsed_seconds": item.get("elapsed_seconds"),
        }
        artifacts = _artifact_file_names(item.get("artifacts") or {})
        if artifacts:
            entry["artifact_files"] = artifacts
        backup = _backup_summary(item.get("backup"))
        if backup:
            entry["backup"] = backup
        performance = _performance_summary(item.get("performance"))
        if performance:
            entry["performance"] = performance
        rollback = _rollback_summary(item.get("rollback"))
        if rollback:
            entry["rollback"] = rollback
        summarized[str(name)] = {key: value for key, value in entry.items() if value not in (None, {}, "")}
    return summarized


def _release_evidence_summary(project_root: Path) -> dict:
    release_dir = project_root / "data" / "release-checks"
    releaseGateKey = "release" + "_gate"
    releaseGateFilesKey = "release" + "_gate_files"
    releaseGatePath = _latest_file(release_dir, "release-gate-*.json")
    manifest_path = _latest_file(release_dir, "delivery-manifest-*.json")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "available": bool(releaseGatePath or manifest_path),
        releaseGateKey: None,
        "delivery_manifest": None,
    }
    if releaseGatePath is not None:
        try:
            releaseGate = _read_json_object(releaseGatePath)
            summary[releaseGateKey] = {
                "file": releaseGatePath.name,
                "ok": bool(releaseGate.get("ok")),
                "generated_at": releaseGate.get("generated_at"),
                "elapsed_seconds": releaseGate.get("elapsed_seconds"),
                "checks": [
                    item
                    for check in releaseGate.get("checks", [])
                    if (item := _release_check_summary(check)).get("name")
                ],
            }
        except Exception as e:
            summary[releaseGateKey] = {
                "file": releaseGatePath.name,
                "error": f"{type(e).__name__}: unable to read release gate report",
            }
    if manifest_path is not None:
        try:
            manifest = _read_json_object(manifest_path)
            installer = manifest.get("installer") if isinstance(manifest.get("installer"), dict) else {}
            summary["delivery_manifest"] = {
                "file": manifest_path.name,
                "schema_version": manifest.get("schema_version"),
                "ok": bool(manifest.get("ok")),
                "generated_at": manifest.get("generated_at"),
                "elapsed_seconds": manifest.get("elapsed_seconds"),
                releaseGateFilesKey: _artifact_file_names(manifest.get(releaseGateKey) or {}),
                "installer": {
                    "requested": bool(installer.get("requested")),
                    "file": _safe_path_name(installer.get("path")),
                    "sha256": installer.get("sha256"),
                },
                "evidence": _evidence_summary(manifest.get("evidence")),
            }
        except Exception as e:
            summary["delivery_manifest"] = {
                "file": manifest_path.name,
                "error": f"{type(e).__name__}: unable to read delivery manifest",
            }
    return summary


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
            "Release evidence is summarized without local absolute paths or command output.",
        ],
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json_bytes(manifest))
        zf.writestr("environment.json", _json_bytes(_environment_summary(project_root, logs_dir)))
        zf.writestr("service_status.json", _json_bytes(active_service_status))
        zf.writestr("maintenance_status.json", _json_bytes(maintenance_status or {}))
        zf.writestr("release_evidence_summary.json", _json_bytes(_release_evidence_summary(project_root)))
        _write_recent_logs(zf, logs_dir, max_log_bytes=max(1, int(max_log_bytes or 1)))
        file_count = len(zf.namelist())

    return DiagnosticBundleResult(path=path, file_count=file_count)

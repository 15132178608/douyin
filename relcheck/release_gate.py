"""One-command release acceptance gate and report writer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Callable, Sequence

from src.config import PROJECT_ROOT, settings


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "release-checks"
DOWNLOAD_ROOT = Path("D:/codexDownload/douyinclaude-release-gate")
DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP = 8
DEFAULT_BENCHMARKS_DIR = PROJECT_ROOT / "data" / "benchmarks"
DEFAULT_DIAGNOSTICS_DIR = PROJECT_ROOT / "data" / "diagnostics"
RELEASE_EVIDENCE_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})")
RELEASE_CHECK_REPORT_PATTERNS = (
    "release-gate-*.json",
    "release-gate-*.md",
    "delivery-manifest-*.json",
    "delivery-manifest-*.md",
    "pre-release-backup-*.json",
    "doctor-report-*.json",
    "delivery-evidence-check*.json",
    "delivery-evidence-check*.md",
    "preflight-summary*.json",
    "preflight-summary*.md",
    "final-release-check*.json",
    "final-release-check*.md",
)
BENCHMARK_REPORT_PATTERNS = (
    "web-benchmark-*.json",
    "web-benchmark-*.md",
)
DIAGNOSTIC_REPORT_PATTERNS = (
    "douyin-recall-diagnostics-*.zip",
)
PROTECTED_EVIDENCE_SUFFIXES = (".db", ".db-wal", ".db-shm")


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    exit_code: int
    elapsed_seconds: float
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path, dict[str, str], int], CommandResult]
PerformanceChecker = Callable[[Path, bool], dict]
PreReleaseBackupChecker = Callable[[Path], dict]
ManifestRollbackChecker = Callable[[Path], dict]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _default_env() -> dict[str, str]:
    env = os.environ.copy()
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    env.setdefault("UV_CACHE_DIR", str(DOWNLOAD_ROOT / "uv-cache"))
    env.setdefault("UV_LINK_MODE", "copy")
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DOWNLOAD_ROOT / "ms-playwright"))
    test_deps = Path("D:/codexDownload/douyinclaude-test-deps")
    if test_deps.exists():
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(test_deps) if not current else f"{test_deps}{os.pathsep}{current}"
    return env


def run_command(command: Sequence[str], cwd: Path, env: dict[str, str], timeout_seconds: int) -> CommandResult:
    start = time.perf_counter()
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    return CommandResult(
        command=list(command),
        exit_code=int(completed.returncode),
        elapsed_seconds=time.perf_counter() - start,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_file_info(path: Path, *, category: str, pattern: str | None = None) -> dict:
    stat = path.stat()
    info = {
        "category": category,
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if pattern:
        info["pattern"] = pattern
    return info


def _is_protected_evidence_file(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in PROTECTED_EVIDENCE_SUFFIXES)


def _evidence_sort_key(path: Path) -> tuple[int, str, str]:
    stamp = ""
    match = RELEASE_EVIDENCE_TIMESTAMP_RE.search(path.name)
    if match:
        stamp = match.group(1)
    return (path.stat().st_mtime_ns, stamp, path.name)


def _collect_retention_candidates(
    root: Path,
    *,
    category: str,
    patterns: Sequence[str],
    keep_latest: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    if not root.exists():
        return [], [], []
    kept: list[dict] = []
    candidates: list[dict] = []
    seen_candidates: set[Path] = set()
    for pattern in patterns:
        files = [
            path
            for path in root.glob(pattern)
            if path.is_file() and not _is_protected_evidence_file(path)
        ]
        files.sort(key=_evidence_sort_key, reverse=True)
        for path in files[:keep_latest]:
            kept.append(_evidence_file_info(path, category=category, pattern=pattern))
        for path in files[keep_latest:]:
            resolved = path.resolve()
            if resolved in seen_candidates:
                continue
            seen_candidates.add(resolved)
            candidates.append(_evidence_file_info(path, category=category, pattern=pattern))
    protected = [
        _evidence_file_info(path, category=category)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_file() and _is_protected_evidence_file(path)
    ]
    return kept, candidates, protected


def describe_release_evidence_retention(
    *,
    release_checks_dir: Path | str = DEFAULT_OUTPUT_DIR,
    benchmarks_dir: Path | str = DEFAULT_BENCHMARKS_DIR,
    diagnostics_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
    keep_latest: int = DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP,
) -> dict:
    """Return a dry-run report for release evidence report retention."""
    keep = max(1, int(keep_latest or DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP))
    sections = [
        (Path(release_checks_dir), "release_checks", RELEASE_CHECK_REPORT_PATTERNS),
        (Path(benchmarks_dir), "benchmarks", BENCHMARK_REPORT_PATTERNS),
        (Path(diagnostics_dir), "diagnostics", DIAGNOSTIC_REPORT_PATTERNS),
    ]
    kept: list[dict] = []
    candidates: list[dict] = []
    protected: list[dict] = []
    for root, category, patterns in sections:
        section_kept, section_candidates, section_protected = _collect_retention_candidates(
            root,
            category=category,
            patterns=patterns,
            keep_latest=keep,
        )
        kept.extend(section_kept)
        candidates.extend(section_candidates)
        protected.extend(section_protected)
    candidates.sort(key=lambda item: (item["category"], item.get("pattern", ""), item["name"]))
    kept.sort(key=lambda item: (item["category"], item.get("pattern", ""), item["name"]))
    return {
        "ok": True,
        "mode": "dry_run",
        "keep_latest": keep,
        "delete_method": "one_file_at_a_time",
        "release_checks_dir": str(Path(release_checks_dir)),
        "benchmarks_dir": str(Path(benchmarks_dir)),
        "diagnostics_dir": str(Path(diagnostics_dir)),
        "kept": kept,
        "delete_candidates": candidates,
        "protected": protected,
    }


def enforce_release_evidence_retention(
    *,
    release_checks_dir: Path | str = DEFAULT_OUTPUT_DIR,
    benchmarks_dir: Path | str = DEFAULT_BENCHMARKS_DIR,
    diagnostics_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
    keep_latest: int = DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP,
) -> dict:
    """Delete old release evidence reports one explicit file at a time."""
    report = describe_release_evidence_retention(
        release_checks_dir=release_checks_dir,
        benchmarks_dir=benchmarks_dir,
        diagnostics_dir=diagnostics_dir,
        keep_latest=keep_latest,
    )
    deleted: list[dict] = []
    errors: list[dict] = []
    for item in report["delete_candidates"]:
        path = Path(item["path"])
        try:
            if path.exists() and path.is_file() and not _is_protected_evidence_file(path):
                path.unlink()
            deleted.append(item)
        except Exception as exc:
            failed = dict(item)
            failed["error"] = str(exc)
            errors.append(failed)
    applied = dict(report)
    applied["mode"] = "apply"
    applied["ok"] = not errors
    applied["deleted"] = deleted
    applied["errors"] = errors
    return applied


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_table_counts(db_path: Path, tables: Sequence[str]) -> tuple[dict[str, int], list[str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing = [table for table in tables if table not in existing]
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {_sqlite_identifier(table)}").fetchone()[0])
            for table in tables
            if table in existing
        }
        return counts, missing
    finally:
        conn.close()


def _unique_backup_path(backup_dir: Path, stamp: str) -> Path:
    path = backup_dir / f"pre-release-recall-{stamp}.db"
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = backup_dir / f"pre-release-recall-{stamp}-{index}.db"
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法生成唯一的发布前备份文件名。")


def _copy_sqlite_backup(source_path: Path, backup_path: Path) -> None:
    source = sqlite3.connect(str(source_path))
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def check_pre_release_backup(
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    *,
    db_path: Path | str | None = None,
    backup_dir: Path | str | None = None,
) -> dict:
    started = time.perf_counter()
    stamp = _timestamp()
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    source_path = Path(db_path) if db_path is not None else settings.db_path
    from src import maintenance

    backup_root = Path(backup_dir) if backup_dir is not None else maintenance.DEFAULT_BACKUP_DIR
    backup_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / f"pre-release-backup-{stamp}.json"
    backup_path: Path | None = None
    artifacts: dict[str, str] = {"report": str(report_path)}
    backup_report: dict = {
        "path": None,
        "sha256": None,
        "size_bytes": 0,
        "source_counts": {},
        "backup_counts": {},
        "validation": None,
    }
    errors: list[str] = []

    try:
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"源数据库不存在：{source_path}")

        critical_tables = sorted(maintenance.REQUIRED_RESTORE_TABLES)
        source_counts, source_missing = _sqlite_table_counts(source_path, critical_tables)
        if source_missing:
            errors.append("源数据库缺少必要表：" + ", ".join(source_missing))

        backup_path = _unique_backup_path(backup_root, stamp)
        _copy_sqlite_backup(source_path, backup_path)
        artifacts["backup"] = str(backup_path)

        validation = maintenance.validate_sqlite_backup(backup_path)
        backup_counts, backup_missing = _sqlite_table_counts(backup_path, critical_tables)
        if backup_missing:
            errors.append("备份数据库缺少必要表：" + ", ".join(backup_missing))
        if not validation["ok"]:
            errors.extend(validation["errors"])
        if source_counts != backup_counts:
            errors.append("备份关键表数量与源数据库不一致。")

        backup_report = {
            "path": str(backup_path),
            "sha256": _sha256(backup_path),
            "size_bytes": backup_path.stat().st_size if backup_path.exists() else 0,
            "source_counts": source_counts,
            "backup_counts": backup_counts,
            "validation": validation,
        }
        if not backup_report["sha256"] or backup_report["size_bytes"] <= 0:
            errors.append("备份文件为空或无法计算 SHA256。")
    except Exception as exc:
        errors.append(str(exc))

    ok = not errors
    report = {
        "ok": ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(source_path),
        "backup": backup_report,
        "errors": errors,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    check = _internal_check_result(
        name="pre_release_backup",
        ok=ok,
        elapsed_seconds=time.perf_counter() - started,
        stdout="pre-release backup ok" if ok else "",
        stderr="\n".join(errors),
        artifact_paths=artifacts,
    )
    check["backup"] = backup_report
    return check


def _check_result(
    name: str,
    result: CommandResult,
    artifact_paths: dict[str, str] | None = None,
    *,
    stdout_limit: int | None = 8000,
) -> dict:
    stdout = result.stdout if stdout_limit is None else result.stdout[-stdout_limit:]
    return {
        "name": name,
        "ok": result.exit_code == 0,
        "exit_code": result.exit_code,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "command": result.command,
        "stdout": stdout,
        "stderr": result.stderr[-8000:],
        "artifacts": artifact_paths or {},
    }


def _internal_check_result(
    *,
    name: str,
    ok: bool,
    elapsed_seconds: float,
    stdout: str = "",
    stderr: str = "",
    artifact_paths: dict[str, str] | None = None,
    performance: dict | None = None,
) -> dict:
    check = {
        "name": name,
        "ok": bool(ok),
        "exit_code": 0 if ok else 1,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "command": ["internal", name],
        "stdout": stdout,
        "stderr": stderr,
        "artifacts": artifact_paths or {},
    }
    if performance is not None:
        check["performance"] = performance
    return check


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_doctor_json_artifact(report: dict, output_dir: Path, stamp: str) -> Path | None:
    for check in report.get("checks", []):
        if check.get("name") != "doctor_json" or not check.get("stdout"):
            continue
        try:
            payload = json.loads(check["stdout"])
        except json.JSONDecodeError:
            return None
        path = output_dir / f"doctor-report-{stamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        check.setdefault("artifacts", {})["report"] = str(path)
        return path
    return None


def _latest_web_benchmark_path(benchmarks_dir: Path) -> Path | None:
    files = [path for path in benchmarks_dir.glob("web-benchmark-*.json") if path.is_file()]
    if not files:
        return None
    files.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return files[0]


def _extract_performance_snapshot(benchmarks_dir: Path) -> tuple[dict | None, list[str], dict[str, str]]:
    artifacts: dict[str, str] = {}
    errors: list[str] = []
    web_path = _latest_web_benchmark_path(benchmarks_dir)
    query_path = benchmarks_dir / "query-performance-audit.json"
    if web_path is None:
        errors.append("没有找到 web-benchmark-*.json。")
    else:
        artifacts["web_benchmark"] = str(web_path)
    if not query_path.exists():
        errors.append("没有找到 query-performance-audit.json。")
    else:
        artifacts["query_performance"] = str(query_path)
    if errors:
        return None, errors, artifacts

    web_report = _read_json(web_path)
    query_report = _read_json(query_path)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "web_pages": {
            str(page["name"]): {
                "path": page.get("path"),
                "avg_ms": float(page.get("avg_ms") or 0),
            }
            for page in web_report.get("pages", [])
        },
        "queries": {
            str(item["name"]): {
                "after_ms": float(item.get("after_ms") or 0),
            }
            for item in query_report.get("queries", [])
        },
    }
    return snapshot, [], artifacts


def _metric_ms(item: object, key: str) -> float | None:
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, dict) and key in item:
        return float(item[key])
    return None


def _evaluate_performance_regressions(
    current: dict,
    baseline: dict,
    *,
    web_relative_threshold: float = 0.35,
    web_absolute_ms: float = 50.0,
    query_relative_threshold: float = 0.35,
    query_absolute_ms: float = 5.0,
) -> list[dict]:
    regressions: list[dict] = []
    groups = [
        ("web_pages", "avg_ms", web_relative_threshold, web_absolute_ms),
        ("queries", "after_ms", query_relative_threshold, query_absolute_ms),
    ]
    for category, metric_key, relative, absolute in groups:
        current_items = current.get(category, {})
        baseline_items = baseline.get(category, {})
        for name, current_item in current_items.items():
            if name not in baseline_items:
                continue
            current_ms = _metric_ms(current_item, metric_key)
            baseline_ms = _metric_ms(baseline_items[name], metric_key)
            if current_ms is None or baseline_ms is None:
                continue
            allowed_ms = baseline_ms + max(float(absolute), baseline_ms * float(relative))
            if current_ms > allowed_ms:
                regressions.append(
                    {
                        "category": category,
                        "name": name,
                        "metric": metric_key,
                        "baseline_ms": baseline_ms,
                        "current_ms": current_ms,
                        "allowed_ms": allowed_ms,
                        "delta_ms": current_ms - baseline_ms,
                        "relative_increase": (current_ms - baseline_ms) / baseline_ms if baseline_ms > 0 else None,
                    }
                )
    return regressions


def check_performance_regression(
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    *,
    benchmarks_dir: Path | str = PROJECT_ROOT / "data" / "benchmarks",
    update_baseline: bool = False,
) -> dict:
    started = time.perf_counter()
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    baseline_path = output_root / "performance-baseline.json"
    current_path = output_root / "performance-current.json"
    snapshot, errors, artifacts = _extract_performance_snapshot(Path(benchmarks_dir))
    artifacts.update(
        {
            "baseline": str(baseline_path),
            "current": str(current_path),
        }
    )
    if snapshot is None:
        message = "\n".join(errors)
        return _internal_check_result(
            name="performance_regression",
            ok=False,
            elapsed_seconds=time.perf_counter() - started,
            stderr=message,
            artifact_paths=artifacts,
            performance={"baseline_status": "missing_reports", "regressions": []},
        )

    current_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    if update_baseline or not baseline_path.exists():
        baseline_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "updated" if update_baseline else "created"
        return _internal_check_result(
            name="performance_regression",
            ok=True,
            elapsed_seconds=time.perf_counter() - started,
            stdout=f"performance baseline {status}",
            artifact_paths=artifacts,
            performance={"baseline_status": status, "regressions": []},
        )

    baseline = _read_json(baseline_path)
    regressions = _evaluate_performance_regressions(snapshot, baseline)
    if regressions:
        lines = [
            "{category} {name}: current={current_ms:.2f}ms baseline={baseline_ms:.2f}ms allowed={allowed_ms:.2f}ms".format(
                **item
            )
            for item in regressions
        ]
        return _internal_check_result(
            name="performance_regression",
            ok=False,
            elapsed_seconds=time.perf_counter() - started,
            stderr="\n".join(lines),
            artifact_paths=artifacts,
            performance={"baseline_status": "compared", "regressions": regressions},
        )
    return _internal_check_result(
        name="performance_regression",
        ok=True,
        elapsed_seconds=time.perf_counter() - started,
        stdout="performance within baseline thresholds",
        artifact_paths=artifacts,
        performance={"baseline_status": "compared", "regressions": []},
    )


def check_manifest_rollback_dry_run(manifest_path: Path | str) -> dict:
    started = time.perf_counter()
    path = Path(manifest_path)
    from src import maintenance

    rollback = maintenance.restore_from_delivery_manifest(path, apply=False)
    artifacts = {"manifest": str(path)}
    backup_path = ((rollback.get("validation") or {}).get("backup") or {}).get("path")
    if backup_path:
        artifacts["backup"] = str(backup_path)
    check = _internal_check_result(
        name="manifest_rollback_dry_run",
        ok=bool(rollback.get("ok")) and not bool(rollback.get("restored")),
        elapsed_seconds=time.perf_counter() - started,
        stdout="manifest rollback dry-run ok" if rollback.get("ok") else "",
        stderr="\n".join(rollback.get("errors") or []),
        artifact_paths=artifacts,
    )
    check["rollback"] = rollback
    return check


def _write_reports(report: dict, output_dir: Path, stamp: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"release-gate-{stamp}.json"
    md_path = output_dir / f"release-gate-{stamp}.md"
    manifest_json_path = output_dir / f"delivery-manifest-{stamp}.json"
    manifest_md_path = output_dir / f"delivery-manifest-{stamp}.md"
    report["reports"] = {
        "json": str(json_path),
        "markdown": str(md_path),
        "manifest_json": str(manifest_json_path),
        "manifest_markdown": str(manifest_md_path),
    }
    _write_doctor_json_artifact(report, output_dir, stamp)
    manifest = build_delivery_manifest(report)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    manifest_json_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    manifest_md_path.write_text(render_delivery_manifest_markdown(manifest), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report["reports"]


def build_delivery_manifest(report: dict) -> dict:
    evidence = {}
    for check in report.get("checks", []):
        name = check.get("name")
        if not name:
            continue
        evidence[name] = {
            "ok": bool(check.get("ok")),
            "exit_code": int(check.get("exit_code", 1)),
            "elapsed_seconds": check.get("elapsed_seconds"),
            "artifacts": check.get("artifacts") or {},
        }
        if "backup" in check:
            evidence[name]["backup"] = check["backup"]
        if "performance" in check:
            evidence[name]["performance"] = check["performance"]
        if "rollback" in check:
            evidence[name]["rollback"] = check["rollback"]

    reports = report.get("reports", {})
    return {
        "schema_version": 1,
        "generated_at": report.get("generated_at"),
        "ok": bool(report.get("ok")),
        "project_root": report.get("project_root"),
        "download_root": report.get("download_root"),
        "elapsed_seconds": report.get("elapsed_seconds"),
        "release_gate": {
            "json": reports.get("json"),
            "markdown": reports.get("markdown"),
        },
        "installer": report.get("installer") or {},
        "evidence": evidence,
    }


def render_delivery_manifest_markdown(manifest: dict) -> str:
    lines = [
        "# Douyin Recall Delivery Manifest",
        "",
        f"- generated_at: `{manifest.get('generated_at')}`",
        f"- ok: `{manifest.get('ok')}`",
        f"- project_root: `{manifest.get('project_root')}`",
        f"- release_gate_json: `{manifest.get('release_gate', {}).get('json')}`",
        f"- release_gate_markdown: `{manifest.get('release_gate', {}).get('markdown')}`",
        "",
        "## Installer",
        "",
    ]
    installer = manifest.get("installer") or {}
    lines.extend(
        [
            f"- requested: `{installer.get('requested')}`",
            f"- path: `{installer.get('path')}`",
            f"- sha256: `{installer.get('sha256')}`",
            "",
            "## Evidence",
            "",
            "| evidence | ok | exit | artifacts |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for name, item in manifest.get("evidence", {}).items():
        artifacts = item.get("artifacts") or {}
        artifact_text = "<br>".join(f"{label}: `{path}`" for label, path in artifacts.items()) or "-"
        lines.append(f"| {name} | {item.get('ok')} | {item.get('exit_code')} | {artifact_text} |")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown_report(report: dict) -> str:
    lines = [
        "# Douyin Recall Release Gate",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- ok: `{report['ok']}`",
        f"- elapsed_seconds: `{report['elapsed_seconds']}`",
        f"- project_root: `{report['project_root']}`",
        "",
        "| check | ok | exit | seconds |",
        "| --- | --- | ---: | ---: |",
    ]
    for check in report["checks"]:
        lines.append(
            f"| {check['name']} | {check['ok']} | {check['exit_code']} | {check['elapsed_seconds']} |"
        )
    installer = report.get("installer") or {}
    if installer:
        lines.extend(
            [
                "",
                "## Installer",
                "",
                f"- path: `{installer.get('path')}`",
                f"- sha256: `{installer.get('sha256')}`",
            ]
        )
    lines.extend(["", "## Commands", ""])
    for check in report["checks"]:
        lines.append(f"### {check['name']}")
        lines.append("")
        lines.append("```text")
        lines.append(" ".join(check["command"]))
        lines.append("```")
        if check.get("artifacts"):
            lines.append("")
            for label, path in check["artifacts"].items():
                lines.append(f"- {label}: `{path}`")
        if not check["ok"] and check.get("stderr"):
            lines.append("")
            lines.append("```text")
            lines.append(check["stderr"])
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _default_checks(python_executable: str) -> list[tuple[str, list[str], int, dict[str, str]]]:
    installed_smoke_report = PROJECT_ROOT / "data" / "release-checks" / "installed-smoke-report.json"
    return [
        ("pytest", [python_executable, "-m", "pytest", "-q"], 600, {}),
        (
            "doctor_json",
            [python_executable, "-m", "src.cli", "doctor", "--json"],
            120,
            {},
        ),
        (
            "installed_smoke",
            [
                python_executable,
                str(PROJECT_ROOT / "scripts" / "installed_smoke.py"),
                "--app-root",
                str(PROJECT_ROOT / "data" / "release-checks" / "installed-smoke"),
                "--output-dir",
                str(PROJECT_ROOT / "data" / "release-checks"),
            ],
            300,
            {"report": str(installed_smoke_report)},
        ),
        (
            "database_safety_audit",
            [python_executable, str(PROJECT_ROOT / "scripts" / "database_safety_audit.py")],
            300,
            {"report": str(PROJECT_ROOT / "data" / "audits" / "database-safety-audit.json")},
        ),
        (
            "backup_restore_drill",
            [python_executable, str(PROJECT_ROOT / "scripts" / "backup_restore_drill.py")],
            300,
            {"report": str(PROJECT_ROOT / "data" / "audits" / "backup-restore-drill" / "backup-restore-drill.json")},
        ),
        (
            "web_benchmark",
            [python_executable, str(PROJECT_ROOT / "scripts" / "benchmark_web_pages.py")],
            300,
            {"report_dir": str(PROJECT_ROOT / "data" / "benchmarks")},
        ),
        (
            "query_performance_audit",
            [python_executable, str(PROJECT_ROOT / "scripts" / "query_performance_audit.py")],
            300,
            {"report": str(PROJECT_ROOT / "data" / "benchmarks" / "query-performance-audit.md")},
        ),
        (
            "acceptance_matrix",
            [python_executable, str(PROJECT_ROOT / "scripts" / "acceptance_matrix.py")],
            120,
            {
                "json": str(PROJECT_ROOT / "data" / "release-checks" / "acceptance-matrix.json"),
                "markdown": str(PROJECT_ROOT / "data" / "release-checks" / "acceptance-matrix.md"),
            },
        ),
    ]


def run_release_gate(
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    benchmarks_dir: Path | str = DEFAULT_BENCHMARKS_DIR,
    diagnostics_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
    runner: Runner | None = None,
    performance_checker: PerformanceChecker | None = None,
    pre_release_backup_checker: PreReleaseBackupChecker | None = None,
    manifest_rollback_checker: ManifestRollbackChecker | None = None,
    include_installer_build: bool = False,
    installer_path: Path | str | None = None,
    python_executable: str | None = None,
    stop_on_failure: bool = True,
    update_performance_baseline: bool = False,
    cleanup_release_evidence: bool = False,
    release_evidence_keep: int = DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP,
) -> dict:
    start = time.perf_counter()
    stamp = _timestamp()
    output_root = Path(output_dir)
    env = _default_env()
    execute = runner or run_command
    python = python_executable or sys.executable
    checks: list[dict] = []
    ok = True

    backup_check = (pre_release_backup_checker or check_pre_release_backup)(output_root)
    checks.append(backup_check)
    if not backup_check["ok"]:
        ok = False

    if ok or not stop_on_failure:
        for name, command, timeout, artifacts in _default_checks(python):
            result = execute(command, PROJECT_ROOT, env, timeout)
            stdout_limit = None if name == "doctor_json" else 8000
            check = _check_result(name, result, artifacts, stdout_limit=stdout_limit)
            checks.append(check)
            if not check["ok"]:
                ok = False
                if stop_on_failure:
                    break

    if ok:
        check_performance = performance_checker or check_performance_regression
        perf_check = check_performance(output_root, update_baseline=update_performance_baseline)
        checks.append(perf_check)
        if not perf_check["ok"]:
            ok = False

    installer_report = {
        "requested": bool(include_installer_build),
        "path": str(installer_path) if installer_path is not None else None,
        "sha256": None,
    }
    if ok and include_installer_build:
        build_script = PROJECT_ROOT / "packaging" / "windows" / "build-installer.ps1"
        result = execute(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(build_script),
            ],
            PROJECT_ROOT,
            env,
            900,
        )
        checks.append(_check_result("installer_build", result, {"script": str(build_script)}))
        ok = ok and result.exit_code == 0
        candidate = Path(installer_path) if installer_path is not None else PROJECT_ROOT / "packaging" / "windows" / "out" / "DouyinRecallSetup.exe"
        installer_report["path"] = str(candidate)
        installer_report["sha256"] = _sha256(candidate)

    report = {
        "ok": bool(ok),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - start, 3),
        "project_root": str(PROJECT_ROOT),
        "download_root": str(DOWNLOAD_ROOT),
        "checks": checks,
        "installer": installer_report,
        "reports": {},
    }
    _write_reports(report, output_root, stamp)
    if ok:
        check_manifest = manifest_rollback_checker or check_manifest_rollback_dry_run
        rollback_check = check_manifest(Path(report["reports"]["manifest_json"]))
        checks.append(rollback_check)
        if not rollback_check["ok"]:
            ok = False
        report["ok"] = bool(ok)
        report["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        _write_reports(report, output_root, stamp)
    if cleanup_release_evidence:
        retention = enforce_release_evidence_retention(
            release_checks_dir=output_root,
            benchmarks_dir=benchmarks_dir,
            diagnostics_dir=diagnostics_dir,
            keep_latest=release_evidence_keep,
        )
        report["release_evidence_retention"] = retention
        report["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        _write_reports(report, output_root, stamp)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Douyin Recall release gate.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--build-installer", action="store_true")
    parser.add_argument("--installer-path", default=None)
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--update-performance-baseline", action="store_true")
    parser.add_argument("--keep-release-evidence", type=int, default=DEFAULT_RELEASE_EVIDENCE_RETENTION_KEEP)
    parser.add_argument("--skip-evidence-cleanup", action="store_true")
    args = parser.parse_args(argv)

    report = run_release_gate(
        output_dir=Path(args.output_dir),
        include_installer_build=args.build_installer,
        installer_path=Path(args.installer_path) if args.installer_path else None,
        stop_on_failure=not args.continue_on_failure,
        update_performance_baseline=args.update_performance_baseline,
        cleanup_release_evidence=not args.skip_evidence_cleanup,
        release_evidence_keep=args.keep_release_evidence,
    )
    print(f"Release gate report: {report['reports']['markdown']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

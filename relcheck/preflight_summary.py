"""Read-only release preflight summary assembled from existing evidence reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT


DEFAULT_RELEASE_CHECKS_DIR = PROJECT_ROOT / "data" / "release-checks"
DEFAULT_BENCHMARKS_DIR = PROJECT_ROOT / "data" / "benchmarks"
DEFAULT_AUDITS_DIR = PROJECT_ROOT / "data" / "audits"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_file(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    files = [path for path in root.glob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _check(
    *,
    name: str,
    title: str,
    path: Path,
    details: dict[str, Any] | None = None,
    message: str | None = None,
    ok: bool | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        return (
            {
                "name": name,
                "title": title,
                "ok": False,
                "path": str(path),
                "message": "缺少报告文件。",
                "details": details or {},
            },
            [str(path)],
        )

    payload = _read_json(path)
    resolved_ok = bool(payload.get("ok", True)) if ok is None else bool(ok)
    return (
        {
            "name": name,
            "title": title,
            "ok": resolved_ok,
            "path": str(path),
            "message": message or ("通过" if resolved_ok else "报告状态为失败。"),
            "details": details if details is not None else _compact_report_details(payload),
        },
        [],
    )


def _missing_latest_check(name: str, title: str, root: Path, pattern: str) -> tuple[dict[str, Any], list[str]]:
    missing = root / pattern
    return (
        {
            "name": name,
            "title": title,
            "ok": False,
            "path": str(missing),
            "message": f"缺少匹配 {pattern} 的报告文件。",
            "details": {},
        },
        [str(missing)],
    )


def _compact_report_details(payload: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key in (
        "generated_at",
        "elapsed_seconds",
        "summary",
        "manifest_path",
        "failed_checks",
        "mismatches",
        "errors",
    ):
        if key in payload:
            details[key] = payload[key]
    return details


def _release_gate_check(release_checks_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = _latest_file(release_checks_dir, "release-gate-*.json")
    if path is None:
        return _missing_latest_check("release_gate", "发布门禁", release_checks_dir, "release-gate-*.json")
    payload = _read_json(path)
    failed = [item.get("name") for item in payload.get("checks", []) if not item.get("ok", False)]
    details = {
        "generated_at": payload.get("generated_at"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "total_checks": len(payload.get("checks", [])),
        "failed_checks": failed,
    }
    return _check(
        name="release_gate",
        title="发布门禁",
        path=path,
        details=details,
        ok=bool(payload.get("ok", False)),
        message="通过" if payload.get("ok") else "release gate 报告状态为失败。",
    )


def _delivery_evidence_check(release_checks_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = release_checks_dir / "delivery-evidence-check.json"
    if not path.exists():
        return _check(name="delivery_evidence", title="交付证据一致性", path=path)
    payload = _read_json(path)
    details = {
        "summary": payload.get("summary", {}),
        "manifest_path": payload.get("manifest_path"),
        "missing_files": payload.get("missing_files", []),
        "mismatches": payload.get("mismatches", []),
    }
    return _check(
        name="delivery_evidence",
        title="交付证据一致性",
        path=path,
        details=details,
        ok=bool(payload.get("ok", False)),
        message="通过" if payload.get("ok") else "交付证据复核失败。",
    )


def _acceptance_matrix_check(release_checks_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = release_checks_dir / "acceptance-matrix.json"
    if not path.exists():
        return _check(name="acceptance_matrix", title="验收矩阵", path=path)
    payload = _read_json(path)
    details = {
        "summary": payload.get("summary", {}),
        "missing_goals": [
            goal.get("id")
            for goal in payload.get("goals", [])
            if goal.get("status") != "covered" or goal.get("missing")
        ],
    }
    return _check(
        name="acceptance_matrix",
        title="验收矩阵",
        path=path,
        details=details,
        ok=bool(payload.get("ok", False)),
        message="通过" if payload.get("ok") else "验收矩阵存在缺口。",
    )


def _metric_value(payload: dict[str, Any], category: str, name: str, field: str) -> float | None:
    value = payload.get(category, {}).get(name, {}).get(field)
    if value is None:
        return None
    return float(value)


def _performance_section(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    category: str,
    field: str,
) -> dict[str, dict[str, float | None]]:
    names = sorted(set(current.get(category, {})) | set(baseline.get(category, {})))
    return {
        name: {
            "current_ms": _metric_value(current, category, name, field),
            "baseline_ms": _metric_value(baseline, category, name, field),
        }
        for name in names
    }


def _performance_check(release_checks_dir: Path, benchmarks_dir: Path) -> tuple[dict[str, Any], list[str]]:
    current_path = release_checks_dir / "performance-current.json"
    baseline_path = release_checks_dir / "performance-baseline.json"
    missing = [str(path) for path in (current_path, baseline_path) if not path.exists()]
    if missing:
        return (
            {
                "name": "performance",
                "title": "性能基准",
                "ok": False,
                "paths": [str(current_path), str(baseline_path)],
                "message": "缺少性能 current 或 baseline 报告。",
                "details": {},
            },
            missing,
        )

    current = _read_json(current_path)
    baseline = _read_json(baseline_path)
    details = {
        "current_path": str(current_path),
        "baseline_path": str(baseline_path),
        "latest_web_benchmark": str(_latest_file(benchmarks_dir, "web-benchmark-*.json") or ""),
        "latest_query_benchmark": str(_latest_file(benchmarks_dir, "query-performance-audit.json") or ""),
        "web_pages": _performance_section(current, baseline, category="web_pages", field="avg_ms"),
        "queries": _performance_section(current, baseline, category="queries", field="after_ms"),
    }
    return (
        {
            "name": "performance",
            "title": "性能基准",
            "ok": True,
            "paths": [str(current_path), str(baseline_path)],
            "message": "通过",
            "details": details,
        },
        [],
    )


def _database_safety_check(audits_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = audits_dir / "database-safety-audit.json"
    if not path.exists():
        return _check(name="database_safety", title="数据库安全巡检", path=path)
    payload = _read_json(path)
    details = {
        "protected_fields": payload.get("protected_fields", {}),
        "check_count": len(payload.get("checks", [])),
        "failed_checks": [item.get("name") for item in payload.get("checks", []) if item.get("mismatches")],
    }
    return _check(
        name="database_safety",
        title="数据库安全巡检",
        path=path,
        details=details,
        ok=bool(payload.get("ok", False)),
        message="通过" if payload.get("ok") else "数据库安全巡检失败。",
    )


def _backup_restore_check(audits_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = audits_dir / "backup-restore-drill" / "backup-restore-drill.json"
    if not path.exists():
        return _check(name="backup_restore_drill", title="备份恢复演练", path=path)
    payload = _read_json(path)
    details = {
        "backup_path": payload.get("backup_path"),
        "restored_path": payload.get("restored_path"),
        "safety_backup_path": payload.get("safety_backup_path"),
        "compared_tables": payload.get("compared_tables", []),
        "mismatch_count": len(payload.get("mismatches", [])),
        "damage_detected": payload.get("damage", {}).get("detected"),
    }
    return _check(
        name="backup_restore_drill",
        title="备份恢复演练",
        path=path,
        details=details,
        ok=bool(payload.get("ok", False)),
        message="通过" if payload.get("ok") else "备份恢复演练失败。",
    )


def build_preflight_summary(
    *,
    release_checks_dir: Path | str = DEFAULT_RELEASE_CHECKS_DIR,
    benchmarks_dir: Path | str = DEFAULT_BENCHMARKS_DIR,
    audits_dir: Path | str = DEFAULT_AUDITS_DIR,
) -> dict[str, Any]:
    release_root = Path(release_checks_dir)
    benchmark_root = Path(benchmarks_dir)
    audit_root = Path(audits_dir)
    checks: list[dict[str, Any]] = []
    missing_files: list[str] = []

    builders = (
        lambda: _release_gate_check(release_root),
        lambda: _delivery_evidence_check(release_root),
        lambda: _acceptance_matrix_check(release_root),
        lambda: _performance_check(release_root, benchmark_root),
        lambda: _database_safety_check(audit_root),
        lambda: _backup_restore_check(audit_root),
    )
    for builder in builders:
        check, missing = builder()
        checks.append(check)
        missing_files.extend(missing)

    passed = sum(1 for check in checks if check.get("ok"))
    failed = len(checks) - passed
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": failed == 0,
        "summary": {
            "total": len(checks),
            "passed": passed,
            "failed": failed,
        },
        "release_checks_dir": str(release_root),
        "benchmarks_dir": str(benchmark_root),
        "audits_dir": str(audit_root),
        "missing_files": sorted(set(missing_files)),
        "failed_checks": [check["name"] for check in checks if not check.get("ok")],
        "checks": checks,
    }


def _details_summary(check: dict[str, Any]) -> str:
    details = check.get("details", {})
    name = check.get("name")
    if name == "release_gate":
        failed = details.get("failed_checks") or []
        return f"{details.get('total_checks', 0)} 项检查，失败 {len(failed)} 项"
    if name == "delivery_evidence":
        summary = details.get("summary", {})
        return f"{summary.get('passed', 0)}/{summary.get('total', 0)} 项证据通过"
    if name == "acceptance_matrix":
        summary = details.get("summary", {})
        return f"{summary.get('covered', 0)}/{summary.get('total', 0)} 个目标有证据"
    if name == "performance":
        return (
            f"{len(details.get('web_pages', {}))} 个页面，"
            f"{len(details.get('queries', {}))} 条 SQL 指标"
        )
    if name == "database_safety":
        return f"{details.get('check_count', 0)} 个数据保护场景"
    if name == "backup_restore_drill":
        return f"{len(details.get('compared_tables', []))} 张表恢复对比"
    return check.get("message", "")


def render_preflight_summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Douyin Recall 发布前自检摘要",
        "",
        f"- 生成时间: `{report.get('generated_at')}`",
        f"- 总体状态: `{'通过' if report.get('ok') else '失败'}`",
        f"- 检查项: `{report.get('summary', {}).get('passed')}/{report.get('summary', {}).get('total')}`",
        "",
        "## 检查结果",
        "",
        "| 项目 | 状态 | 摘要 | 报告 |",
        "| --- | --- | --- | --- |",
    ]
    for check in report.get("checks", []):
        status = "通过" if check.get("ok") else "失败"
        report_path = check.get("path") or ", ".join(check.get("paths", [])) or "-"
        lines.append(
            f"| {check.get('title')} | {status} | {_details_summary(check)} | `{report_path}` |"
        )

    if report.get("missing_files"):
        lines.extend(["", "## 缺失文件", ""])
        for path in report["missing_files"]:
            lines.append(f"- `{path}`")

    failed_checks = [check for check in report.get("checks", []) if not check.get("ok")]
    if failed_checks:
        lines.extend(["", "## 失败检查", ""])
        for check in failed_checks:
            lines.append(f"- {check.get('title')}: {check.get('message')}")

    return "\n".join(lines).rstrip() + "\n"


def write_preflight_summary(report: dict[str, Any], output_dir: Path | str) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "preflight-summary.json"
    markdown_path = root / "preflight-summary.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_preflight_summary_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}

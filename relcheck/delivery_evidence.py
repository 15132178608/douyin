"""Validate delivery-manifest evidence paths and statuses."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT


DEFAULT_RELEASE_CHECKS_DIR = PROJECT_ROOT / "data" / "release-checks"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _path_value(payload: dict, dotted_key: str) -> str | None:
    value: Any = payload
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if value is None:
        return None
    return str(value)


def _path_exists(path_text: str | None) -> bool:
    return bool(path_text) and Path(path_text).exists()


def _add_path_check(
    *,
    check_name: str,
    label: str,
    path_text: str | None,
    paths: list[dict],
    missing_files: list[str],
    mismatches: list[str],
    expect_dir: bool = False,
) -> None:
    ok = False
    if path_text:
        path = Path(path_text)
        ok = path.is_dir() if expect_dir else path.is_file()
    paths.append({"label": label, "path": path_text, "ok": ok})
    if not ok:
        missing = path_text or "<missing path>"
        missing_files.append(missing)
        mismatches.append(f"{check_name}.{label} missing: {missing}")


def _evidence_status_ok(check_name: str, item: dict | None, mismatches: list[str]) -> bool:
    if item is None:
        mismatches.append(f"{check_name} missing evidence")
        return False
    ok = bool(item.get("ok")) and int(item.get("exit_code", 1)) == 0
    if not ok:
        mismatches.append(
            f"{check_name} status mismatch: ok={item.get('ok')} exit_code={item.get('exit_code')}"
        )
    return ok


def _check_manifest_status(manifest: dict, missing_files: list[str], mismatches: list[str]) -> dict:
    ok = bool(manifest.get("ok")) and int(manifest.get("schema_version", 0)) == 1
    if not ok:
        mismatches.append(
            f"manifest_status mismatch: ok={manifest.get('ok')} schema_version={manifest.get('schema_version')}"
        )
    return {"name": "manifest_status", "ok": ok, "paths": [], "errors": [] if ok else mismatches[-1:]}


def _check_release_gate(manifest: dict, missing_files: list[str], mismatches: list[str]) -> dict:
    paths: list[dict] = []
    _add_path_check(
        check_name="release_gate",
        label="json",
        path_text=_path_value(manifest, "release_gate.json"),
        paths=paths,
        missing_files=missing_files,
        mismatches=mismatches,
    )
    _add_path_check(
        check_name="release_gate",
        label="markdown",
        path_text=_path_value(manifest, "release_gate.markdown"),
        paths=paths,
        missing_files=missing_files,
        mismatches=mismatches,
    )
    return {"name": "release_gate", "ok": all(item["ok"] for item in paths), "paths": paths, "errors": []}


def _check_evidence_paths(
    check_name: str,
    item: dict | None,
    required_paths: list[tuple[str, str, bool]],
    missing_files: list[str],
    mismatches: list[str],
) -> dict:
    paths: list[dict] = []
    status_ok = _evidence_status_ok(check_name, item, mismatches)
    item = item or {}
    for label, dotted_key, expect_dir in required_paths:
        _add_path_check(
            check_name=check_name,
            label=label,
            path_text=_path_value(item, dotted_key),
            paths=paths,
            missing_files=missing_files,
            mismatches=mismatches,
            expect_dir=expect_dir,
        )
    return {"name": check_name, "ok": status_ok and all(path["ok"] for path in paths), "paths": paths, "errors": []}


def _check_web_benchmark(item: dict | None, missing_files: list[str], mismatches: list[str]) -> dict:
    check = _check_evidence_paths(
        "web_benchmark",
        item,
        [("report_dir", "artifacts.report_dir", True)],
        missing_files,
        mismatches,
    )
    report_dir = _path_value(item or {}, "artifacts.report_dir")
    if report_dir and Path(report_dir).is_dir():
        json_reports = sorted(Path(report_dir).glob("web-benchmark-*.json"))
        markdown_reports = sorted(Path(report_dir).glob("web-benchmark-*.md"))
        if not json_reports:
            missing_files.append(str(Path(report_dir) / "web-benchmark-*.json"))
            mismatches.append(f"web_benchmark.json_report missing: {Path(report_dir) / 'web-benchmark-*.json'}")
        if not markdown_reports:
            missing_files.append(str(Path(report_dir) / "web-benchmark-*.md"))
            mismatches.append(f"web_benchmark.markdown_report missing: {Path(report_dir) / 'web-benchmark-*.md'}")
        check["paths"].append({"label": "json_report", "path": str(json_reports[-1]) if json_reports else None, "ok": bool(json_reports)})
        check["paths"].append({"label": "markdown_report", "path": str(markdown_reports[-1]) if markdown_reports else None, "ok": bool(markdown_reports)})
    check["ok"] = check["ok"] and all(path["ok"] for path in check["paths"])
    return check


def _check_query_performance(item: dict | None, missing_files: list[str], mismatches: list[str]) -> dict:
    check = _check_evidence_paths(
        "query_performance_audit",
        item,
        [("report", "artifacts.report", False)],
        missing_files,
        mismatches,
    )
    report_path = _path_value(item or {}, "artifacts.report")
    json_path = str(Path(report_path).with_suffix(".json")) if report_path else None
    _add_path_check(
        check_name="query_performance_audit",
        label="json",
        path_text=json_path,
        paths=check["paths"],
        missing_files=missing_files,
        mismatches=mismatches,
    )
    check["ok"] = check["ok"] and all(path["ok"] for path in check["paths"])
    return check


def _check_manifest_rollback(item: dict | None, missing_files: list[str], mismatches: list[str]) -> dict:
    check = _check_evidence_paths(
        "manifest_rollback_dry_run",
        item,
        [("manifest", "artifacts.manifest", False), ("backup", "artifacts.backup", False)],
        missing_files,
        mismatches,
    )
    rollback = (item or {}).get("rollback") or {}
    rollback_ok = bool(rollback.get("ok")) and rollback.get("mode") == "dry_run" and rollback.get("restored") is False
    if not rollback_ok:
        mismatches.append(
            "manifest_rollback_dry_run.rollback mismatch: "
            f"ok={rollback.get('ok')} mode={rollback.get('mode')} restored={rollback.get('restored')}"
        )
    check["ok"] = check["ok"] and rollback_ok
    return check


def validate_delivery_manifest_evidence(manifest_path: Path | str) -> dict:
    path = Path(manifest_path)
    missing_files: list[str] = []
    mismatches: list[str] = []
    if not path.exists():
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": str(path),
            "ok": False,
            "summary": {"total": 1, "passed": 0, "failed": 1},
            "checks": [],
            "missing_files": [str(path)],
            "mismatches": [f"manifest missing: {path}"],
        }

    manifest = _read_json(path)
    evidence = manifest.get("evidence") or {}
    checks = [
        _check_manifest_status(manifest, missing_files, mismatches),
        _check_release_gate(manifest, missing_files, mismatches),
        _check_evidence_paths(
            "pre_release_backup",
            evidence.get("pre_release_backup"),
            [
                ("report", "artifacts.report", False),
                ("artifact_backup", "artifacts.backup", False),
                ("backup_path", "backup.path", False),
            ],
            missing_files,
            mismatches,
        ),
        _check_evidence_paths(
            "doctor_json",
            evidence.get("doctor_json"),
            [("report", "artifacts.report", False)],
            missing_files,
            mismatches,
        ),
        _check_evidence_paths(
            "installed_smoke",
            evidence.get("installed_smoke"),
            [("report", "artifacts.report", False)],
            missing_files,
            mismatches,
        ),
        _check_evidence_paths(
            "database_safety_audit",
            evidence.get("database_safety_audit"),
            [("report", "artifacts.report", False)],
            missing_files,
            mismatches,
        ),
        _check_evidence_paths(
            "backup_restore_drill",
            evidence.get("backup_restore_drill"),
            [("report", "artifacts.report", False)],
            missing_files,
            mismatches,
        ),
        _check_web_benchmark(evidence.get("web_benchmark"), missing_files, mismatches),
        _check_query_performance(evidence.get("query_performance_audit"), missing_files, mismatches),
        _check_evidence_paths(
            "acceptance_matrix",
            evidence.get("acceptance_matrix"),
            [("json", "artifacts.json", False), ("markdown", "artifacts.markdown", False)],
            missing_files,
            mismatches,
        ),
        _check_evidence_paths(
            "performance_regression",
            evidence.get("performance_regression"),
            [
                ("web_benchmark", "artifacts.web_benchmark", False),
                ("query_performance", "artifacts.query_performance", False),
                ("baseline", "artifacts.baseline", False),
                ("current", "artifacts.current", False),
            ],
            missing_files,
            mismatches,
        ),
        _check_manifest_rollback(evidence.get("manifest_rollback_dry_run"), missing_files, mismatches),
    ]
    passed = sum(1 for check in checks if check["ok"])
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(path),
        "ok": passed == len(checks),
        "summary": {"total": len(checks), "passed": passed, "failed": len(checks) - passed},
        "checks": checks,
        "missing_files": sorted(set(missing_files)),
        "mismatches": mismatches,
    }


def find_latest_delivery_manifest(root: Path | str = DEFAULT_RELEASE_CHECKS_DIR) -> Path | None:
    base = Path(root)
    if not base.exists():
        return None
    files = [path for path in base.glob("delivery-manifest-*.json") if path.is_file()]
    if not files:
        return None
    files.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    return files[0]


def render_delivery_evidence_markdown(report: dict) -> str:
    lines = [
        "# Douyin Recall Delivery Evidence Check",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- manifest_path: `{report.get('manifest_path')}`",
        f"- total: `{report.get('summary', {}).get('total')}`",
        f"- passed: `{report.get('summary', {}).get('passed')}`",
        f"- failed: `{report.get('summary', {}).get('failed')}`",
        "",
        "## Checks",
        "",
        "| check | ok | paths |",
        "| --- | --- | --- |",
    ]
    for check in report.get("checks", []):
        paths = "<br>".join(
            f"{path['label']}: `{path.get('path')}`"
            for path in check.get("paths", [])
        ) or "-"
        lines.append(f"| {check['name']} | {check['ok']} | {paths} |")
    if report.get("mismatches"):
        lines.extend(["", "## Mismatches", ""])
        for item in report["mismatches"]:
            lines.append(f"- {item}")
    return "\n".join(lines).rstrip() + "\n"


def write_delivery_evidence_report(report: dict, output_dir: Path | str) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "delivery-evidence-check.json"
    markdown_path = root / "delivery-evidence-check.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_delivery_evidence_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}

"""Final release check orchestration for existing release verification commands."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence

from src.config import PROJECT_ROOT


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "release-checks"
DEFAULT_BENCHMARKS_DIR = PROJECT_ROOT / "data" / "benchmarks"
DEFAULT_AUDITS_DIR = PROJECT_ROOT / "data" / "audits"
DOWNLOAD_ROOT = Path("D:/codexDownload/douyinclaude-release-gate")


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    exit_code: int
    elapsed_seconds: float
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path, dict[str, str], int], CommandResult]


def _default_env() -> dict[str, str]:
    env = os.environ.copy()
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
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


def _latest_file(root: Path, pattern: str) -> str | None:
    if not root.exists():
        return None
    files = [path for path in root.glob(pattern) if path.is_file()]
    if not files:
        return None
    return str(max(files, key=lambda path: (path.stat().st_mtime_ns, path.name)))


def _step_artifacts(name: str, output_dir: Path) -> dict[str, str]:
    if name == "release_gate":
        return {
            "release_gate_json": _latest_file(output_dir, "release-gate-*.json") or "",
            "release_gate_markdown": _latest_file(output_dir, "release-gate-*.md") or "",
            "delivery_manifest": _latest_file(output_dir, "delivery-manifest-*.json") or "",
        }
    if name == "delivery_evidence":
        return {
            "json": str(output_dir / "delivery-evidence-check.json"),
            "markdown": str(output_dir / "delivery-evidence-check.md"),
        }
    if name == "preflight_summary":
        return {
            "json": str(output_dir / "preflight-summary.json"),
            "markdown": str(output_dir / "preflight-summary.md"),
        }
    return {}


def _release_gate_command(
    *,
    python_executable: str,
    output_dir: Path,
    build_installer: bool,
    installer_path: Path | None,
    update_performance_baseline: bool,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "relcheck.release_gate",
        "--output-dir",
        str(output_dir),
        "--skip-evidence-cleanup",
    ]
    if build_installer:
        command.append("--build-installer")
    if installer_path is not None:
        command.extend(["--installer-path", str(installer_path)])
    if update_performance_baseline:
        command.append("--update-performance-baseline")
    return command


def _planned_steps(
    *,
    python_executable: str,
    output_dir: Path,
    benchmarks_dir: Path,
    audits_dir: Path,
    build_installer: bool,
    installer_path: Path | None,
    update_performance_baseline: bool,
) -> list[dict]:
    return [
        {
            "name": "release_gate",
            "title": "发布门禁",
            "timeout_seconds": 900,
            "command": _release_gate_command(
                python_executable=python_executable,
                output_dir=output_dir,
                build_installer=build_installer,
                installer_path=installer_path,
                update_performance_baseline=update_performance_baseline,
            ),
        },
        {
            "name": "delivery_evidence",
            "title": "交付证据复核",
            "timeout_seconds": 120,
            "command": [
                python_executable,
                str(PROJECT_ROOT / "scripts" / "validate_delivery_evidence.py"),
                "--output-dir",
                str(output_dir),
            ],
        },
        {
            "name": "preflight_summary",
            "title": "发布前自检摘要",
            "timeout_seconds": 120,
            "command": [
                python_executable,
                str(PROJECT_ROOT / "scripts" / "preflight_summary.py"),
                "--release-checks-dir",
                str(output_dir),
                "--benchmarks-dir",
                str(benchmarks_dir),
                "--audits-dir",
                str(audits_dir),
                "--output-dir",
                str(output_dir),
            ],
        },
    ]


def _result_step(step: dict, result: CommandResult, output_dir: Path) -> dict:
    ok = result.exit_code == 0
    return {
        "name": step["name"],
        "title": step["title"],
        "ok": ok,
        "status": "passed" if ok else "failed",
        "exit_code": result.exit_code,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "command": result.command,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "message": "通过" if ok else "命令返回非 0。",
        "artifacts": _step_artifacts(step["name"], output_dir),
    }


def _skipped_step(step: dict) -> dict:
    return {
        "name": step["name"],
        "title": step["title"],
        "ok": False,
        "status": "skipped",
        "exit_code": None,
        "elapsed_seconds": 0.0,
        "command": step["command"],
        "stdout": "",
        "stderr": "",
        "message": "前置检查失败，未执行。",
        "artifacts": {},
    }


def run_final_release_check(
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    benchmarks_dir: Path | str = DEFAULT_BENCHMARKS_DIR,
    audits_dir: Path | str = DEFAULT_AUDITS_DIR,
    python_executable: str | None = None,
    build_installer: bool = False,
    installer_path: Path | str | None = None,
    update_performance_baseline: bool = False,
    runner: Runner = run_command,
) -> dict:
    output_root = Path(output_dir)
    benchmark_root = Path(benchmarks_dir)
    audit_root = Path(audits_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    executable = python_executable or sys.executable
    env = _default_env()
    steps: list[dict] = []
    blocked = False

    for step in _planned_steps(
        python_executable=executable,
        output_dir=output_root,
        benchmarks_dir=benchmark_root,
        audits_dir=audit_root,
        build_installer=build_installer,
        installer_path=Path(installer_path) if installer_path is not None else None,
        update_performance_baseline=update_performance_baseline,
    ):
        if blocked:
            steps.append(_skipped_step(step))
            continue
        result = runner(step["command"], PROJECT_ROOT, env, int(step["timeout_seconds"]))
        check = _result_step(step, result, output_root)
        steps.append(check)
        if not check["ok"]:
            blocked = True

    passed = sum(1 for step in steps if step["status"] == "passed")
    failed = sum(1 for step in steps if step["status"] == "failed")
    skipped = sum(1 for step in steps if step["status"] == "skipped")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": failed == 0 and skipped == 0,
        "summary": {
            "total": len(steps),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "output_dir": str(output_root),
        "benchmarks_dir": str(benchmark_root),
        "audits_dir": str(audit_root),
        "failed_steps": [step["name"] for step in steps if step["status"] == "failed"],
        "steps": steps,
    }


def render_final_release_check_markdown(report: dict) -> str:
    lines = [
        "# Douyin Recall 发布终检",
        "",
        f"- 生成时间: `{report.get('generated_at')}`",
        f"- 总体状态: `{'通过' if report.get('ok') else '失败'}`",
        f"- 检查项: `{report.get('summary', {}).get('passed')}/{report.get('summary', {}).get('total')}`",
        f"- 跳过: `{report.get('summary', {}).get('skipped')}`",
        "",
        "## 步骤",
        "",
        "| 项目 | 状态 | 退出码 | 耗时 | 报告 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for step in report.get("steps", []):
        artifact_text = "<br>".join(
            f"{key}: `{value}`" for key, value in step.get("artifacts", {}).items() if value
        ) or "-"
        status = {"passed": "通过", "failed": "失败", "skipped": "跳过"}.get(step.get("status"), step.get("status"))
        exit_code = "-" if step.get("exit_code") is None else step.get("exit_code")
        lines.append(
            f"| {step.get('title')} | {status} | {exit_code} | {step.get('elapsed_seconds')} | {artifact_text} |"
        )

    failed = [step for step in report.get("steps", []) if step.get("status") != "passed"]
    if failed:
        lines.extend(["", "## 未通过步骤", ""])
        for step in failed:
            lines.append(f"- {step.get('title')}: {step.get('message')}")
    return "\n".join(lines).rstrip() + "\n"


def write_final_release_check_report(report: dict, output_dir: Path | str) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "final-release-check.json"
    markdown_path = root / "final-release-check.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_final_release_check_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}

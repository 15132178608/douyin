"""Acceptance coverage matrix for the long-running hardening goal."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from src.config import PROJECT_ROOT


GOAL_SPECS = (
    {
        "id": "regression_tests",
        "title": "回归测试补强",
        "completion_goal": "核心页面状态、退出登录、账号切换和已有数据展示改坏时测试失败。",
        "tests": [
            "tests/test_web_likes_module.py",
            "tests/test_web_multitenant.py",
            "tests/test_crawler_api.py",
            "tests/test_operational_scripts.py",
        ],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/web/app.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest -q", "python -m relcheck.release_gate --skip-evidence-cleanup"],
        "notes": "全量 pytest 和安装冒烟共同覆盖最近出现过的核心回归面。",
    },
    {
        "id": "performance_benchmark",
        "title": "性能基准脚本",
        "completion_goal": "一键输出首页、收藏、喜欢、分类、维护、账号页响应耗时，并可对比退化。",
        "tests": ["tests/test_operational_scripts.py"],
        "scripts": ["scripts/benchmark_web_pages.py"],
        "source_files": ["relcheck/performance_benchmark.py", "relcheck/release_gate.py"],
        "release_gate_checks": ["web_benchmark", "performance_regression"],
        "report_artifacts": [
            "data/benchmarks/web-benchmark-*.json",
            "data/benchmarks/web-benchmark-*.md",
            "data/release-checks/performance-current.json",
        ],
        "verification": ["python scripts/benchmark_web_pages.py", "python -m relcheck.release_gate --skip-evidence-cleanup"],
        "notes": "release gate 会生成当前性能快照，并和 baseline 比较。",
    },
    {
        "id": "database_safety_audit",
        "title": "数据库安全巡检",
        "completion_goal": "自动验证同步、退出登录、切换账号、分类导入、备份恢复不会删除关键数据。",
        "tests": ["tests/test_operational_scripts.py"],
        "scripts": ["scripts/database_safety_audit.py"],
        "source_files": ["relcheck/database_safety.py"],
        "release_gate_checks": ["database_safety_audit"],
        "report_artifacts": ["data/audits/database-safety-audit.json"],
        "verification": ["python scripts/database_safety_audit.py"],
        "notes": "巡检报告包含操作前后数量和受保护字段对比。",
    },
    {
        "id": "job_queue_stability",
        "title": "后台任务队列稳定化",
        "completion_goal": "重复入队抑制、失败重试、running 恢复和页面状态一致性都有自动验证。",
        "tests": ["tests/test_jobs.py", "tests/test_maintenance.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/jobs.py", "src/maintenance.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_jobs.py tests/test_maintenance.py -q"],
        "notes": "安装冒烟会断言重复任务数、重试状态、running 恢复和页面/后台一致性。",
    },
    {
        "id": "sync_idempotency",
        "title": "同步幂等性测试",
        "completion_goal": "同一批收藏/喜欢反复同步不会重复插入，也不会覆盖本地备注、分类和时间字段。",
        "tests": ["tests/test_sync.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/crawler/sync.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_sync.py -q"],
        "notes": "安装冒烟同时覆盖 favorites 和 likes 的重复同步保护。",
    },
    {
        "id": "category_import_migration",
        "title": "分类导入与迁移增强",
        "completion_goal": "能发现旧库分类、匹配当前收藏导入、不覆盖现有分类，并输出数量统计。",
        "tests": ["tests/test_category_import.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/category_import.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_category_import.py -q"],
        "notes": "安装冒烟会复跑迁移，验证旧库匹配、未匹配项和已有分类保护。",
    },
    {
        "id": "diagnostics_layering",
        "title": "诊断信息分层",
        "completion_goal": "用户页面只显示简洁中文提示，路径、截图、堆栈和命令行细节只进日志或诊断包。",
        "tests": ["tests/test_diagnostics.py", "tests/test_web_templates.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/release_gate.py"],
        "source_files": ["src/diagnostics.py", "src/web/app.py", "src/cli.py"],
        "release_gate_checks": ["pytest", "doctor_json"],
        "report_artifacts": ["data/diagnostics/douyin-recall-diagnostics-*.zip"],
        "verification": ["python -m pytest tests/test_diagnostics.py tests/test_web_templates.py -q"],
        "notes": "模板和诊断包测试覆盖本机路径、命令行和敏感文件脱敏。",
    },
    {
        "id": "backup_restore_drill",
        "title": "备份恢复演练自动化",
        "completion_goal": "脚本创建测试库、备份、模拟损坏、恢复，并指出表或字段不一致。",
        "tests": ["tests/test_operational_scripts.py", "tests/test_maintenance.py"],
        "scripts": ["scripts/backup_restore_drill.py"],
        "source_files": ["relcheck/backup_drill.py", "src/maintenance.py"],
        "release_gate_checks": ["backup_restore_drill", "manifest_rollback_dry_run", "pre_release_backup"],
        "report_artifacts": ["data/audits/backup-restore-drill/backup-restore-drill.json"],
        "verification": ["python scripts/backup_restore_drill.py"],
        "notes": "release gate 同时保留发布前回滚点和 manifest dry-run 校验。",
    },
    {
        "id": "account_boundaries",
        "title": "账号体系逻辑整理",
        "completion_goal": "添加账号、切换、退出、重新绑定边界清楚，退出不删本地内容，多账号隔离。",
        "tests": ["tests/test_accounts.py", "tests/test_web_multitenant.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/accounts.py", "src/web/app.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_accounts.py tests/test_web_multitenant.py -q"],
        "notes": "安装冒烟覆盖多会话切换、退出保留内容和重新绑定只影响当前用户。",
    },
    {
        "id": "query_performance",
        "title": "慢查询和索引优化",
        "completion_goal": "上千/上万模拟数据覆盖列表、搜索、分类、作者页，补索引后可复测。",
        "tests": ["tests/test_db_schema.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/query_performance_audit.py"],
        "source_files": ["relcheck/query_performance.py", "src/db.py"],
        "release_gate_checks": ["query_performance_audit", "performance_regression"],
        "report_artifacts": [
            "data/benchmarks/query-performance-audit.json",
            "data/benchmarks/query-performance-audit.md",
        ],
        "verification": ["python scripts/query_performance_audit.py"],
        "notes": "SQL 审计报告记录 before/after 耗时、查询计划和索引命中。",
    },
    {
        "id": "crawler_state_machine",
        "title": "Crawler 状态机测试",
        "completion_goal": "二维码、等待扫码、已扫码待确认、成功、取消、超时、登录失效都有 mock 测试。",
        "tests": ["tests/test_crawler_api.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/crawler/douyin.py", "src/web/app.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_crawler_api.py -q"],
        "notes": "状态机 trace helper 和安装冒烟 auth fragment 不需要真实手机扫码。",
    },
    {
        "id": "maintenance_backend_capabilities",
        "title": "维护中心后端能力补强",
        "completion_goal": "后端稳定返回服务、登录、失败任务、备份、索引、建议动作，页面可复用。",
        "tests": ["tests/test_maintenance.py", "tests/test_operational_scripts.py"],
        "scripts": ["scripts/installed_smoke.py"],
        "source_files": ["src/maintenance.py", "src/web/app.py", "relcheck/installed_smoke.py"],
        "release_gate_checks": ["pytest", "installed_smoke"],
        "verification": ["python -m pytest tests/test_maintenance.py -q"],
        "notes": "维护状态包含 capabilities_schema_version 和稳定能力键。",
    },
)


def _path_exists(project_root: Path, path: str) -> bool:
    if "*" in path:
        return bool(list(project_root.glob(path)))
    return (project_root / path).exists()


def _evidence_status(project_root: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    for item in paths:
        if _path_exists(project_root, item):
            present.append(item)
        else:
            missing.append(item)
    return present, missing


def build_acceptance_matrix(project_root: Path | str = PROJECT_ROOT) -> dict:
    root = Path(project_root)
    goals: list[dict] = []
    for spec in GOAL_SPECS:
        tests, missing_tests = _evidence_status(root, list(spec.get("tests", [])))
        scripts, missing_scripts = _evidence_status(root, list(spec.get("scripts", [])))
        source_files, missing_sources = _evidence_status(root, list(spec.get("source_files", [])))
        report_artifacts, missing_artifacts = _evidence_status(root, list(spec.get("report_artifacts", [])))
        missing = [
            *(f"test:{item}" for item in missing_tests),
            *(f"script:{item}" for item in missing_scripts),
            *(f"source:{item}" for item in missing_sources),
        ]
        status = "covered" if not missing and tests and spec.get("verification") else "missing"
        goals.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "status": status,
                "completion_goal": spec["completion_goal"],
                "tests": tests,
                "scripts": scripts,
                "source_files": source_files,
                "release_gate_checks": list(spec.get("release_gate_checks", [])),
                "report_artifacts": report_artifacts,
                "missing_report_artifacts": missing_artifacts,
                "verification": list(spec.get("verification", [])),
                "notes": spec.get("notes", ""),
                "missing": missing,
            }
        )

    covered = sum(1 for goal in goals if goal["status"] == "covered")
    summary = {
        "total": len(goals),
        "covered": covered,
        "missing": len(goals) - covered,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "ok": summary["missing"] == 0,
        "summary": summary,
        "verification_commands": {
            "pytest": "python -m pytest -q",
            "release_gate": "python -m relcheck.release_gate --skip-evidence-cleanup",
            "acceptance_matrix": "python scripts/acceptance_matrix.py",
        },
        "goals": goals,
    }


def render_acceptance_matrix_markdown(report: dict) -> str:
    lines = [
        "# Douyin Recall Acceptance Matrix",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- total: `{report.get('summary', {}).get('total')}`",
        f"- covered: `{report.get('summary', {}).get('covered')}`",
        f"- missing: `{report.get('summary', {}).get('missing')}`",
        "",
        "## Verification Commands",
        "",
    ]
    for name, command in report.get("verification_commands", {}).items():
        lines.append(f"- {name}: `{command}`")
    lines.extend(
        [
            "",
            "## Goals",
            "",
            "| goal | status | tests | release gate | reports |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for goal in report.get("goals", []):
        tests = "<br>".join(f"`{item}`" for item in goal.get("tests", [])) or "-"
        checks = "<br>".join(f"`{item}`" for item in goal.get("release_gate_checks", [])) or "-"
        reports = "<br>".join(f"`{item}`" for item in goal.get("report_artifacts", [])) or "-"
        lines.append(f"| {goal['title']} | {goal['status']} | {tests} | {checks} | {reports} |")

    missing_goals = [goal for goal in report.get("goals", []) if goal.get("missing")]
    if missing_goals:
        lines.extend(["", "## Missing Evidence", ""])
        for goal in missing_goals:
            lines.append(f"- {goal['title']}: {', '.join(goal['missing'])}")
    return "\n".join(lines).rstrip() + "\n"


def write_acceptance_matrix_reports(report: dict, output_dir: Path | str) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "acceptance-matrix.json"
    markdown_path = root / "acceptance-matrix.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_acceptance_matrix_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}

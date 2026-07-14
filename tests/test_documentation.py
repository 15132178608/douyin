"""Documentation-to-code consistency checks for user-facing project guides."""

from __future__ import annotations

import re
from pathlib import Path

from src.cli import cli
from src.maintenance import DEFAULT_BACKUP_RETENTION_KEEP


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_cli_table_matches_registered_commands() -> None:
    readme = read("README.md")
    section_match = re.search(
        r"^## CLI 命令一览（(?P<count>\d+) 个）\s*$"
        r"(?P<body>.*?)^---\s*$",
        readme,
        re.MULTILINE | re.DOTALL,
    )

    assert section_match is not None
    documented_commands = re.findall(
        r"^\| `([^`]+)` \|",
        section_match.group("body"),
        re.MULTILINE,
    )
    registered_commands = set(cli.commands)

    assert len(documented_commands) == len(set(documented_commands))
    assert set(documented_commands) == registered_commands
    assert int(section_match.group("count")) == len(registered_commands)


def test_architecture_describes_current_web_ownership_and_job_flow() -> None:
    architecture = read("docs/architecture.md")
    from src.web.app import app

    documented_route_modules = set(
        re.findall(r"^\| `([a-z_]+\.py)` \|", architecture, re.MULTILINE)
    )
    actual_route_modules = {
        f"{endpoint_module.rsplit('.', 1)[-1]}.py"
        for route in app.routes
        if (endpoint_module := getattr(route.endpoint, "__module__", "")).startswith(
            "src.web.routes."
        )
    }
    assert documented_route_modules == actual_route_modules

    assert "item_action_service" in architecture
    assert "job_queue" in architecture
    assert "PersistentUncollectWorker" in architecture
    assert not re.search(r"当前基线：.*v\d+", architecture)
    for stale_description in (
        "17 commands",
        "app.py        FastAPI 路由",
        "web.app: 调用 PersistentUncollectWorker",
        "/status/uncollect-bridge",
        "web/routes/content.py",
    ):
        assert stale_description not in architecture

    route_source = read("src/web/routes/item_actions.py")
    service_source = read("src/web/item_action_service.py")
    jobs_source = read("src/jobs.py")
    assert "item_action_service.queue_item_removals(" in route_source
    assert "jobs.enqueue_job(" in service_source
    assert "connection=conn" in service_source
    assert "PersistentUncollectWorker(" in jobs_source
    assert "profile_path=accounts.profile_path_for_user(user_id)" in jobs_source
    assert "worker.close()" in jobs_source


def test_historical_specs_are_clearly_archived_on_the_first_screen() -> None:
    initial_spec = read("douyin-recall-spec.md")
    tenancy_memo = read("docs/multi-tenant-roadmap.md")

    assert "历史" in "\n".join(initial_spec.splitlines()[:20])
    assert "不代表当前实现" in "\n".join(initial_spec.splitlines()[:20])
    assert "docs/architecture.md" in "\n".join(initial_spec.splitlines()[:20])
    assert "docs/roadmap.md" in "\n".join(initial_spec.splitlines()[:20])

    tenancy_first_screen = "\n".join(tenancy_memo.splitlines()[:35])
    assert "历史" in tenancy_first_screen
    assert "本地多账号" in tenancy_first_screen
    assert "公网" in tenancy_first_screen
    assert "SMTP" in tenancy_first_screen


def test_backup_and_uninstall_guide_states_verified_boundaries() -> None:
    guide = read("docs/data-backup-and-uninstall.md")
    troubleshooting = read("docs/windows-troubleshooting.md")
    installer = read("packaging/windows/DouyinRecall.iss")

    for required_text in (
        "%LOCALAPPDATA%\\Programs\\DouyinRecall",
        "D:\\codexDownload\\douyinclaude-runtime",
        "recall-backup-*.db",
        "pre-install-recall-*.db",
        "pre-restore-recall-*.db",
        "pre-release-recall-*.db",
        "verify-backup",
        "prune-backups",
        "DB_PATH",
        "PLAYWRIGHT_PROFILE_PATH",
        "USER_DATA_ROOT",
        "AVATAR_CACHE_DIR",
        "DouyinRecallWeeklyMaintenance",
        "recall.db",
    ):
        assert required_text in guide

    assert "每周" in guide and "不会自动" in guide
    assert f"最近 {DEFAULT_BACKUP_RETENTION_KEEP} 份" in guide
    assert "只展示普通" in guide
    assert "best-effort" in guide
    assert "verify-backup --path data\\exports\\recall-backup-" in guide
    assert guide.index("**卸载前**记录") < guide.index("已安装的应用")
    assert "JSON / Markdown" in guide
    assert "重新扫码" in guide
    assert "data-backup-and-uninstall.md" in troubleshooting

    assert "data\\*" in installer
    assert ".env" in installer
    assert ".venv\\*" in installer
    assert "[UninstallDelete]" not in installer


def test_roadmap_distinguishes_completed_and_remaining_operations_work() -> None:
    roadmap = read("docs/roadmap.md")

    assert "本地多账号" in roadmap
    assert "公网多人" in roadmap
    assert f"最近 {DEFAULT_BACKUP_RETENTION_KEEP} 份" in roadmap
    assert "恢复" in roadmap and "worker" in roadmap
    assert "队列可读" in roadmap
    assert "worker 存活" in roadmap
    assert "自动下载" in roadmap
    assert "data-backup-and-uninstall.md" in roadmap


def test_current_and_archived_guides_have_valid_local_links() -> None:
    document_paths = (
        ROOT / "README.md",
        ROOT / "douyin-recall-spec.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "roadmap.md",
        ROOT / "docs" / "multi-tenant-roadmap.md",
        ROOT / "docs" / "data-backup-and-uninstall.md",
        ROOT / "docs" / "windows-troubleshooting.md",
    )

    missing_targets: list[str] = []
    for document_path in document_paths:
        document = document_path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative_target = target.split("#", 1)[0]
            if not relative_target:
                continue
            resolved_target = (document_path.parent / relative_target).resolve()
            if not resolved_target.exists():
                missing_targets.append(f"{document_path.name}: {target}")

    assert not missing_targets, missing_targets

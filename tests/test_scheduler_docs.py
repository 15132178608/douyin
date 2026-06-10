"""
Windows scheduled automation artifact tests.

Run:
    python tests/test_scheduler_docs.py
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_weekly_maintenance_script_runs_full_pipeline() -> None:
    script = ROOT / "scripts" / "run-weekly-maintenance.ps1"
    text = script.read_text(encoding="utf-8")

    assert "uv run recall crawl" in text
    assert "uv run recall crawl-likes" in text
    assert "uv run recall index --kind favorites" in text
    assert "uv run recall index --kind likes" in text
    assert "uv run recall digest --kind favorites" in text
    assert "uv run recall export --format sqlite" in text


def test_task_installer_registers_weekly_windows_task() -> None:
    script = ROOT / "scripts" / "install-weekly-task.ps1"
    text = script.read_text(encoding="utf-8")

    assert "Register-ScheduledTask" in text
    assert "New-ScheduledTaskTrigger" in text
    assert "Weekly" in text
    assert "run-weekly-maintenance.ps1" in text


def test_scheduler_doc_references_scripts_and_manual_test_command() -> None:
    doc = ROOT / "docs" / "windows-task-scheduler.md"
    text = doc.read_text(encoding="utf-8")

    assert "scripts\\install-weekly-task.ps1" in text
    assert "scripts\\run-weekly-maintenance.ps1" in text
    assert "Get-ScheduledTask" in text
    assert "uv run recall export --format sqlite" in text


if __name__ == "__main__":
    tests = [
        test_weekly_maintenance_script_runs_full_pipeline,
        test_task_installer_registers_weekly_windows_task,
        test_scheduler_doc_references_scripts_and_manual_test_command,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            print(f"FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)

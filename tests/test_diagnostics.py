"""
Diagnostic bundle tests.

Run:
    python tests/test_diagnostics.py
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile

from src import diagnostics


def test_diagnostic_bundle_excludes_sensitive_files_and_redacts_logs() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        logs_dir = root / "data" / "logs"
        output_dir = root / "data" / "diagnostics"
        (root / "data" / "playwright_profile").mkdir(parents=True)
        (root / "data" / "users" / "alice" / "playwright_profile").mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        (root / ".env").write_text("SMTP_PASSWORD=secret-password\n", encoding="utf-8")
        (root / "data" / "recall.db").write_bytes(b"sqlite")
        (root / "data" / "playwright_profile" / "Cookies").write_text("cookie", encoding="utf-8")
        (logs_dir / "recall.log").write_text(
            "mail=person@example.com password=secret-password token=abc123\n"
            "Authorization: Bearer super-secret-token\n"
            "Cookie: sessionid=private-session\n"
            "normal line\n",
            encoding="utf-8",
        )

        result = diagnostics.create_diagnostic_bundle(
            output_dir,
            project_root=root,
            logs_dir=logs_dir,
            service_status={"state": "stopped", "running": False},
            maintenance_status={"jobs": {"failed": 0}, "backups": {"count": 0}},
        )

        assert result.path.exists()
        assert result.path.suffix == ".zip"
        with zipfile.ZipFile(result.path) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "environment.json" in names
            assert "service_status.json" in names
            assert "maintenance_status.json" in names
            assert "logs/recall.log.txt" in names
            joined = "\n".join(sorted(names))
            assert ".env" not in joined
            assert "recall.db" not in joined
            assert "playwright_profile" not in joined
            assert "data/users" not in joined
            log_text = zf.read("logs/recall.log.txt").decode("utf-8")
            assert "person@example.com" not in log_text
            assert "secret-password" not in log_text
            assert "super-secret-token" not in log_text
            assert "private-session" not in log_text
            assert "<redacted-email>" in log_text
            assert "password=<redacted>" in log_text
            assert "Authorization: <redacted>" in log_text
            assert "Cookie: <redacted>" in log_text
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert "excluded_sensitive_paths" in manifest
            assert result.file_count == len(names)


def test_diagnostic_bundle_includes_redacted_release_evidence_summary() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        release_dir = root / "data" / "release-checks"
        output_dir = root / "data" / "diagnostics"
        logs_dir = root / "data" / "logs"
        release_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        local_root = r"C:\Users\Alice\Douyin Recall"
        backup_path = local_root + r"\data\exports\pre-release-recall-secret.db"

        (release_dir / "release-gate-20260707-010000.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "generated_at": "2026-07-07T01:00:00+00:00",
                    "elapsed_seconds": 12.3,
                    "project_root": local_root,
                    "download_root": r"D:\codexDownload\douyinclaude-release-gate",
                    "checks": [
                        {
                            "name": "pytest",
                            "ok": True,
                            "exit_code": 0,
                            "elapsed_seconds": 9.5,
                            "command": [local_root + r"\.venv\Scripts\python.exe", "-m", "pytest"],
                            "stdout": "ok from " + local_root,
                            "stderr": "",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (release_dir / "delivery-manifest-20260707-010000.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": True,
                    "generated_at": "2026-07-07T01:00:00+00:00",
                    "project_root": local_root,
                    "download_root": r"D:\codexDownload\douyinclaude-release-gate",
                    "release_gate": {
                        "json": local_root + r"\data\release-checks\release-gate-20260707-010000.json",
                        "markdown": local_root + r"\data\release-checks\release-gate-20260707-010000.md",
                    },
                    "installer": {
                        "requested": True,
                        "path": local_root + r"\packaging\windows\out\DouyinRecallSetup.exe",
                        "sha256": "installer-sha",
                    },
                    "evidence": {
                        "pre_release_backup": {
                            "ok": True,
                            "exit_code": 0,
                            "elapsed_seconds": 0.5,
                            "artifacts": {
                                "backup": backup_path,
                                "report": local_root + r"\data\release-checks\pre-release-backup.json",
                            },
                            "backup": {
                                "path": backup_path,
                                "sha256": "backup-sha",
                                "size_bytes": 4096,
                                "source_counts": {"favorites": 2, "likes": 3, "users": 1},
                                "backup_counts": {"favorites": 2, "likes": 3, "users": 1},
                                "validation": {"ok": True, "path": backup_path, "errors": []},
                            },
                        },
                        "manifest_rollback_dry_run": {
                            "ok": True,
                            "exit_code": 0,
                            "elapsed_seconds": 0.2,
                            "artifacts": {
                                "manifest": local_root + r"\data\release-checks\delivery-manifest-20260707-010000.json",
                                "backup": backup_path,
                            },
                            "rollback": {
                                "ok": True,
                                "mode": "dry_run",
                                "restored": False,
                                "validation": {
                                    "backup": {
                                        "path": backup_path,
                                        "expected_sha256": "backup-sha",
                                        "actual_sha256": "backup-sha",
                                        "expected_counts": {"favorites": 2, "likes": 3, "users": 1},
                                        "backup_counts": {"favorites": 2, "likes": 3, "users": 1},
                                        "validation": {"ok": True, "path": backup_path, "errors": []},
                                    }
                                },
                            },
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = diagnostics.create_diagnostic_bundle(
            output_dir,
            project_root=root,
            logs_dir=logs_dir,
            service_status={"state": "stopped"},
            maintenance_status={"ok": True},
        )

        with zipfile.ZipFile(result.path) as zf:
            names = set(zf.namelist())
            assert "release_evidence_summary.json" in names
            summary = json.loads(zf.read("release_evidence_summary.json").decode("utf-8"))
            text = json.dumps(summary, ensure_ascii=False)

        assert summary["available"] is True
        assert summary["release_gate"]["file"] == "release-gate-20260707-010000.json"
        assert summary["release_gate"]["checks"] == [
            {"name": "pytest", "ok": True, "exit_code": 0, "elapsed_seconds": 9.5}
        ]
        manifest = summary["delivery_manifest"]
        assert manifest["file"] == "delivery-manifest-20260707-010000.json"
        assert manifest["installer"] == {
            "requested": True,
            "file": "DouyinRecallSetup.exe",
            "sha256": "installer-sha",
        }
        backup = manifest["evidence"]["pre_release_backup"]["backup"]
        assert backup["file"] == "pre-release-recall-secret.db"
        assert backup["sha256"] == "backup-sha"
        assert backup["source_counts"] == {"favorites": 2, "likes": 3, "users": 1}
        assert manifest["evidence"]["manifest_rollback_dry_run"]["rollback"]["restored"] is False
        assert r"C:\Users" not in text
        assert r"D:\codexDownload" not in text
        assert "Douyin Recall\\data" not in text
        assert "python.exe" not in text
        assert "stdout" not in text


if __name__ == "__main__":
    tests = [
        test_diagnostic_bundle_excludes_sensitive_files_and_redacts_logs,
        test_diagnostic_bundle_includes_redacted_release_evidence_summary,
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

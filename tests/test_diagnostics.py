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


if __name__ == "__main__":
    tests = [test_diagnostic_bundle_excludes_sensitive_files_and_redacts_logs]
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

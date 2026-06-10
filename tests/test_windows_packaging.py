from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"


def read(name: str) -> str:
    return (PACKAGING / name).read_text(encoding="utf-8")


class WindowsPackagingTests(unittest.TestCase):
    def test_inno_installer_uses_per_user_install_and_excludes_private_data(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\DouyinRecall", script)
        self.assertIn("data\\*", script)
        self.assertIn(".env", script)
        self.assertIn(".venv\\*", script)
        self.assertIn("AGENTS.md", script)
        self.assertIn(".git\\*", script)

    def test_launcher_prepares_runtime_and_opens_local_web_ui(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("Copy-Item", launcher)
        self.assertIn(".env.example", launcher)
        self.assertIn("uv sync", launcher)
        self.assertIn("playwright install chromium", launcher)
        self.assertIn("uv run recall serve", launcher)
        self.assertIn("http://127.0.0.1:", launcher)

    def test_build_script_requires_inno_setup_and_creates_setup_exe(self) -> None:
        build = read("build-installer.ps1")

        self.assertIn("ISCC.exe", build)
        self.assertIn("DouyinRecall.iss", build)
        self.assertIn("DouyinRecallSetup.exe", build)
        self.assertIn("packaging\\windows\\out", build)


if __name__ == "__main__":
    unittest.main()

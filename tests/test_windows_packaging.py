from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"


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
        self.assertNotIn("createallsubdirs", script)

    def test_inno_version_matches_project_version(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        script = read("DouyinRecall.iss")

        project_version = re.search(r'^version = "([^"]+)"', project, re.MULTILINE)
        installer_version = re.search(r'^#define MyAppVersion "([^"]+)"', script, re.MULTILINE)

        self.assertIsNotNone(project_version)
        self.assertIsNotNone(installer_version)
        self.assertEqual(project_version.group(1), installer_version.group(1))

    def test_launcher_prepares_runtime_and_opens_local_web_ui(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("D:\\codexDownload", launcher)
        self.assertIn("$DownloadRoot", launcher)
        self.assertIn("$env:UV_CACHE_DIR", launcher)
        self.assertIn("$env:PLAYWRIGHT_BROWSERS_PATH", launcher)
        self.assertNotIn("$env:TEMP", launcher)
        self.assertIn("Copy-Item", launcher)
        self.assertIn(".env.example", launcher)
        self.assertIn("uv sync", launcher)
        self.assertIn("playwright install chromium", launcher)
        self.assertIn("uv run recall status", launcher)
        self.assertIn("uv run recall serve", launcher)
        self.assertIn("http://127.0.0.1:", launcher)

    def test_build_script_requires_inno_setup_and_creates_setup_exe(self) -> None:
        build = read("build-installer.ps1")

        self.assertIn("ISCC.exe", build)
        self.assertIn("DouyinRecall.iss", build)
        self.assertIn("DouyinRecallSetup.exe", build)
        self.assertIn("packaging\\windows\\out", build)

    def test_workflow_publishes_setup_exe_to_github_release_on_version_tags(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("tags:", workflow)
        self.assertIn("'v*'", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("packaging/windows/out/DouyinRecallSetup.exe", workflow)


if __name__ == "__main__":
    unittest.main()

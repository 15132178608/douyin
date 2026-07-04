from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"
RELEASE_NOTES_DIR = ROOT / "docs" / "releases"
WINDOWS_TROUBLESHOOTING = ROOT / "docs" / "windows-troubleshooting.md"


def read(name: str) -> str:
    return (PACKAGING / name).read_text(encoding="utf-8")


def project_version() -> str:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', project, re.MULTILINE)
    assert match is not None
    return match.group(1)


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
        self.assertIn("首次启动会下载 Python 依赖和 Playwright 浏览器", launcher)
        self.assertIn("Windows SmartScreen 可能提示风险", launcher)
        self.assertIn("$DownloadRoot", launcher)
        self.assertIn("$env:UV_CACHE_DIR", launcher)
        self.assertIn('$env:UV_LINK_MODE = "copy"', launcher)
        self.assertIn("$env:PLAYWRIGHT_BROWSERS_PATH", launcher)
        self.assertNotIn("$env:TEMP", launcher)
        self.assertIn("Copy-Item", launcher)
        self.assertIn(".env.example", launcher)
        self.assertIn("uv sync", launcher)
        self.assertIn("playwright install chromium", launcher)
        self.assertIn("uv run recall status", launcher)
        self.assertIn("uv run recall serve", launcher)
        self.assertIn("http://127.0.0.1:", launcher)

    def test_launcher_prints_recovery_steps_when_startup_fails(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("$StartLog", launcher)
        self.assertIn("start-douyin-recall.log", launcher)
        self.assertIn("Write-Troubleshooting", launcher)
        self.assertIn("常用恢复命令", launcher)
        self.assertIn("uv run recall status", launcher)
        self.assertIn("uv run recall stop", launcher)
        self.assertIn("uv run recall diagnose", launcher)
        self.assertIn("http://127.0.0.1:$port/maintenance", launcher)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", launcher)

    def test_launcher_runs_startup_preflight_before_downloading_runtime(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("function Test-DirectoryWritable", launcher)
        self.assertIn("function Test-WebEndpoint", launcher)
        self.assertIn("function Assert-StartupPreflight", launcher)
        self.assertIn("安装目录可写", launcher)
        self.assertIn("运行时缓存目录可写", launcher)
        self.assertIn("uv 下载入口可访问", launcher)
        self.assertIn("https://astral.sh/uv/install.ps1", launcher)
        self.assertIn("请检查 D:\\codexDownload 的写入权限", launcher)
        self.assertIn("请检查网络、代理或防火墙", launcher)
        self.assertIn("Remove-Item -LiteralPath $ProbePath -Force", launcher)
        self.assertLess(launcher.index("Assert-StartupPreflight"), launcher.index("$uv = Find-Uv"))

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
        self.assertIn("CHANGELOG.md", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn('"release", "create"', workflow)
        self.assertIn("& gh @releaseArgs", workflow)
        self.assertIn("docs/releases/${env:GITHUB_REF_NAME}.md", workflow)
        self.assertIn("--notes-file", workflow)
        self.assertIn("packaging/windows/out/DouyinRecallSetup.exe", workflow)

    def test_release_notes_document_installer_caveats_and_local_ops(self) -> None:
        version = project_version()
        notes_path = RELEASE_NOTES_DIR / f"v{version}.md"
        self.assertTrue(notes_path.exists(), f"missing release notes: {notes_path}")
        notes = notes_path.read_text(encoding="utf-8")

        self.assertIn(f"v{version}", notes)
        self.assertIn("未签名", notes)
        self.assertIn("SmartScreen", notes)
        self.assertIn("首次启动", notes)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", notes)
        self.assertIn("UV_LINK_MODE", notes)
        self.assertIn("启动前健康检查", notes)
        self.assertIn("recall stop", notes)
        self.assertIn("/maintenance", notes)

    def test_windows_troubleshooting_doc_covers_installer_recovery(self) -> None:
        self.assertTrue(WINDOWS_TROUBLESHOOTING.exists())
        doc = WINDOWS_TROUBLESHOOTING.read_text(encoding="utf-8")

        self.assertIn("Windows 安装包排障", doc)
        self.assertIn("SmartScreen", doc)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", doc)
        self.assertIn("start-douyin-recall.log", doc)
        self.assertIn("uv run recall status", doc)
        self.assertIn("uv run recall stop", doc)
        self.assertIn("uv run recall diagnose", doc)
        self.assertIn("启动前健康检查", doc)
        self.assertIn("/maintenance", doc)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"
RELEASE_NOTES_DIR = ROOT / "docs" / "releases"
WINDOWS_TROUBLESHOOTING = ROOT / "docs" / "windows-troubleshooting.md"


def read(name: str) -> str:
    return (PACKAGING / name).read_text(encoding="utf-8")


def read_bytes(name: str) -> bytes:
    return (PACKAGING / name).read_bytes()


def read_script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


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

    def test_inno_installer_always_shows_directory_selection_page(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("DisableDirPage=no", script)

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
        self.assertIn("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8", launcher)
        self.assertIn("$OutputEncoding = [System.Text.Encoding]::UTF8", launcher)
        self.assertIn('$env:PYTHONUTF8 = "1"', launcher)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', launcher)
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
        self.assertIn("uv run python -m src.cli serve", launcher)
        self.assertIn("http://127.0.0.1:", launcher)

    def test_wscript_launcher_hides_desktop_startup_console(self) -> None:
        launcher = read("launch-douyin-recall.vbs")

        self.assertIn("WindowsPowerShell\\v1.0\\powershell.exe", launcher)
        self.assertIn("start-douyin-recall.ps1", launcher)
        self.assertIn("-WindowStyle Hidden", launcher)
        self.assertIn("-Silent", launcher)
        self.assertIn("shell.Run cmd, 0, False", launcher)

    def test_launcher_prints_coarse_startup_progress(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("$script:StartupStepTotal", launcher)
        self.assertIn("$script:StartupStepIndex", launcher)
        self.assertIn("function Write-StartupProgress", launcher)
        self.assertIn("进度：[$script:StartupStepIndex/$script:StartupStepTotal]", launcher)
        self.assertIn("首次运行可能需要几分钟", launcher)
        self.assertIn("准备本地运行目录", launcher)
        self.assertIn("准备 Python 依赖", launcher)
        self.assertIn("准备 Playwright Chromium", launcher)
        self.assertIn("初始化本地数据库", launcher)

    def test_launcher_keeps_runtime_preparation_page_hidden_during_normal_startup(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("$StartupStatusPath", launcher)
        self.assertIn("function Write-StartupStatusPage", launcher)
        self.assertIn("function Update-StartupStatus", launcher)
        self.assertNotIn("function Show-StartupStatusPage", launcher)
        self.assertNotIn("Start-Process $StartupStatusPath", launcher)
        self.assertNotIn("-OpenPage", launcher)
        self.assertIn("正在准备 Douyin Recall", launcher)
        self.assertIn("检查本地环境", launcher)
        self.assertIn("准备 Python 运行环境", launcher)
        self.assertIn("下载/安装 Playwright Chromium", launcher)
        self.assertIn("初始化本地数据库", launcher)
        self.assertIn("启动本地 Web 服务", launcher)
        self.assertIn("准备完成", launcher)
        self.assertIn("准备失败", launcher)
        self.assertIn('meta http-equiv="refresh" content="2"', launcher)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", launcher)
        self.assertIn("uv run python -m src.cli diagnose", launcher)
        self.assertIn("Douyin Recall Prepare Runtime", launcher)

    def test_launcher_waits_for_web_endpoint_before_opening_browser(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("function Wait-WebReady", launcher)
        self.assertIn("Invoke-WebRequest -Uri $Url", launcher)
        self.assertIn("Timeout waiting for Douyin Recall Web service", launcher)
        self.assertIn("-PassThru", launcher)
        self.assertIn("function Start-RecallServiceProcess", launcher)
        self.assertIn("$serverProcess = Start-RecallServiceProcess", launcher)
        self.assertIn("Start-Process -FilePath", launcher)
        self.assertIn("$Process.HasExited", launcher)
        self.assertNotIn("Start-Sleep -Seconds 3", launcher)
        self.assertLess(launcher.index("Wait-WebReady"), launcher.index("Start-Process $openUrl"))

    def test_launcher_waits_briefly_for_first_run_qr_before_opening_browser(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("function Wait-SetupQrReady", launcher)
        self.assertIn('/setup/auth-status', launcher)
        self.assertIn('/setup', launcher)
        self.assertIn('/auth/qr-image', launcher)
        self.assertIn('data-auth-success', launcher)
        self.assertNotIn("Wait-SetupQrReady -BaseUrl $url -OpenPath $OpenPath", launcher)

    def test_launcher_has_fast_path_for_existing_or_prepared_runtime(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("$RuntimePreparedPath", launcher)
        self.assertIn("$VenvPython", launcher)
        self.assertIn("function Test-RuntimePrepared", launcher)
        self.assertIn("function Write-RuntimePreparedMarker", launcher)
        self.assertIn("function Start-RecallServiceProcess", launcher)
        self.assertIn("运行环境已准备，跳过 uv sync 和 Playwright 安装", launcher)
        self.assertIn("Douyin Recall is already running; opening browser without runtime preparation", launcher)
        self.assertIn("Start-RecallServiceProcess -UsePreparedRuntime", launcher)
        self.assertLess(
            launcher.index("Douyin Recall is already running; opening browser without runtime preparation"),
            launcher.index("准备 Python 运行环境：uv sync"),
        )

    def test_launcher_silent_mode_does_not_wait_for_hidden_console_input(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("[switch]$Silent", launcher)
        self.assertIn("if (-not $Silent)", launcher)
        self.assertIn('Read-Host "Press Enter to close"', launcher)

    def test_launcher_uses_module_cli_entrypoint_for_non_ascii_install_paths(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("function Invoke-RecallCli", launcher)
        self.assertIn("function Get-RecallCliStartInfo", launcher)
        self.assertIn('"run", "python", "-m", "src.cli"', launcher)
        self.assertIn('Invoke-RecallCli @("init-db")', launcher)
        self.assertIn('Get-RecallCliStartInfo @("serve")', launcher)
        self.assertNotIn('& $uv "run" "recall"', launcher)
        self.assertNotIn('"run", "recall", "serve"', launcher)

    def test_launcher_uses_utf8_bom_for_windows_powershell_5_1(self) -> None:
        raw_launcher = read_bytes("start-douyin-recall.ps1")
        launcher = raw_launcher.decode("utf-8-sig")

        self.assertTrue(
            raw_launcher.startswith(b"\xef\xbb\xbf"),
            "start-douyin-recall.ps1 contains Chinese text and must include a UTF-8 BOM for Windows PowerShell 5.1",
        )
        self.assertIn("正在准备 Douyin Recall", launcher)
        self.assertIn("检查本地环境", launcher)

    def test_launcher_prints_recovery_steps_when_startup_fails(self) -> None:
        launcher = read("start-douyin-recall.ps1")

        self.assertIn("$StartLog", launcher)
        self.assertIn("start-douyin-recall.log", launcher)
        self.assertIn("Write-Troubleshooting", launcher)
        self.assertIn("$script:CurrentStartupStep", launcher)
        self.assertIn("function Write-StartupFailureHint", launcher)
        self.assertIn("失败阶段", launcher)
        self.assertIn("可能原因", launcher)
        self.assertIn("建议下一步", launcher)
        self.assertIn("Douyin Recall Prepare Runtime", launcher)
        self.assertIn("常用恢复命令", launcher)
        self.assertIn("uv run python -m src.cli status", launcher)
        self.assertIn("uv run python -m src.cli stop", launcher)
        self.assertIn("uv run python -m src.cli diagnose", launcher)
        self.assertIn("uv sync", launcher)
        self.assertIn("playwright install chromium", launcher)
        self.assertIn("python -m src.cli init-db", launcher)
        self.assertIn("python -m src.cli serve", launcher)
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

    def test_control_script_exposes_local_operations_without_hidden_runtime_paths(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn('[ValidateSet("menu", "start", "prepare", "stop", "status", "maintenance", "auth", "diagnose", "logs", "update", "health", "repair", "backup", "backups", "restore", "verify-backup")]', control)
        self.assertIn("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8", control)
        self.assertIn("$OutputEncoding = [System.Text.Encoding]::UTF8", control)
        self.assertIn('$env:PYTHONUTF8 = "1"', control)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', control)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", control)
        self.assertIn("$UvDownloadDir", control)
        self.assertIn("$env:UV_CACHE_DIR", control)
        self.assertIn('$env:UV_LINK_MODE = "copy"', control)
        self.assertIn("$env:PLAYWRIGHT_BROWSERS_PATH", control)
        self.assertNotIn("$env:TEMP", control)
        self.assertIn('$ExportsDir = Join-Path $AppRoot "data\\exports"', control)
        self.assertIn("Start-DouyinRecall", control)
        self.assertIn("Open-MaintenanceCenter", control)
        self.assertIn("Open-LogsDirectory", control)
        self.assertIn("Invoke-RecallCommand @('status')", control)
        self.assertIn("Invoke-RecallCommand @('stop')", control)
        self.assertIn("Invoke-RecallCommand @('diagnose')", control)
        self.assertIn("Invoke-RecallCommand @('update')", control)
        self.assertIn("Invoke-RecallCommand @('export', '--format', 'sqlite', '--output', $ExportsDir)", control)
        self.assertIn("Invoke-RecallCommand @('verify-backup', '--output', $ExportsDir)", control)
        self.assertIn("/maintenance", control)
        self.assertIn("-OpenPath \"/auth\"", control)
        self.assertIn("start-douyin-recall.ps1", control)
        self.assertIn("Read-Host \"Press Enter to close\"", control)

    def test_control_script_uses_module_cli_entrypoint_for_non_ascii_install_paths(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn('& $uv "run" "python" "-m" "src.cli" @RecallArgs', control)
        self.assertIn("uv run python -m src.cli", control)
        self.assertNotIn('& $uv "run" "recall"', control)

    def test_control_script_exposes_retryable_runtime_preparation(self) -> None:
        control = read("control-douyin-recall.ps1")
        script = read("DouyinRecall.iss")

        self.assertIn('"prepare"', control)
        self.assertIn("function Find-OrInstall-Uv", control)
        self.assertIn("function Invoke-PrepareStep", control)
        self.assertIn("function Prepare-Runtime", control)
        self.assertIn("$script:CurrentPrepareStep", control)
        self.assertIn("function Write-PrepareFailureHint", control)
        self.assertIn("Prepare failed at step:", control)
        self.assertIn("Likely cause:", control)
        self.assertIn("Recommended next step:", control)
        self.assertIn("Retry entry: Douyin Recall Prepare Runtime", control)
        self.assertIn("uv run python -m src.cli diagnose", control)
        self.assertIn("$UvDownloadDir", control)
        self.assertIn("$UvInstallScriptUrl", control)
        self.assertIn("https://astral.sh/uv/install.ps1", control)
        self.assertIn("Invoke-WebRequest -Uri $UvInstallScriptUrl -OutFile $installer", control)
        self.assertIn("uv sync", control)
        self.assertIn("playwright install chromium", control)
        self.assertIn("python -m src.cli init-db", control)
        self.assertIn("python -m src.cli status", control)
        self.assertIn("does not start the local web service", control)
        self.assertIn("Runtime cache:", control)
        self.assertIn('"prepare" { Prepare-Runtime; Wait-BeforeExit }', control)
        self.assertNotIn('"prepare" { Start-DouyinRecall', control)
        self.assertIn("Douyin Recall Prepare Runtime", control)
        self.assertIn("Douyin Recall Prepare Runtime", script)
        self.assertIn('-Action ""prepare""', script)
        self.assertNotIn("Remove-Item -Recurse", control)
        self.assertNotIn("rm -rf", control)

    def test_prepare_runtime_prints_coarse_progress(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("$script:PrepareStepTotal", control)
        self.assertIn("$script:PrepareStepIndex", control)
        self.assertIn("function Write-PrepareProgress", control)
        self.assertIn("Step $script:PrepareStepIndex/$script:PrepareStepTotal", control)
        self.assertIn("This step can take several minutes on first run.", control)
        self.assertIn("Prepare step: $Name", control)
        self.assertIn("uv discovery and install", control)
        self.assertIn("Python dependencies", control)
        self.assertIn("Browser runtime", control)
        self.assertIn("Local database", control)

    def test_prepare_runtime_prints_completion_summary(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("function Write-PrepareCompletionSummary", control)
        self.assertIn("Runtime preparation summary", control)
        self.assertIn("Prepared steps: $script:PrepareStepIndex/$script:PrepareStepTotal", control)
        self.assertIn("Install directory:", control)
        self.assertIn("Runtime cache:", control)
        self.assertIn("Browser cache:", control)
        self.assertIn("Logs:", control)
        self.assertIn("Local web service: not started by this prepare action", control)
        self.assertIn("Next step: Use Douyin Recall to start the web UI when needed.", control)
        self.assertIn("Stop entry: Douyin Recall Stop Service", control)

    def test_control_script_is_ascii_for_windows_powershell_5(self) -> None:
        control = read("control-douyin-recall.ps1")

        try:
            control.encode("ascii")
        except UnicodeEncodeError as exc:
            self.fail(f"control script must stay ASCII for Windows PowerShell 5.1 parsing: {exc}")

    def test_control_script_runs_health_check_and_safe_stale_state_repair(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("function Test-DirectoryWritable", control)
        self.assertIn("function Test-UvAvailable", control)
        self.assertIn("function Get-PortOwnerPid", control)
        self.assertIn("function Get-ServiceAudit", control)
        self.assertIn("function Invoke-HealthCheck", control)
        self.assertIn("function Repair-StaleServerState", control)
        self.assertIn("Douyin Recall Health Check", control)
        self.assertIn("Douyin Recall Repair State", control)
        self.assertIn("Health check", control)
        self.assertIn("Install directory", control)
        self.assertIn("Logs directory", control)
        self.assertIn("Runtime cache", control)
        self.assertIn("uv availability", control)
        self.assertIn("Service record", control)
        self.assertIn("Service audit:", control)
        self.assertIn("Port listener", control)
        self.assertIn("Port owner PID:", control)
        self.assertIn("Next step:", control)
        self.assertIn("own service running", control)
        self.assertIn("stale service record", control)
        self.assertIn("external listener", control)
        self.assertIn("recorded PID and port owner mismatch", control)
        self.assertIn("Do not stop pid=", control)
        self.assertIn("Repair suggestion", control)
        self.assertIn("Remove-Item -LiteralPath $ServerStatePath -Force", control)
        self.assertIn("Remove-Item -LiteralPath $ServerPidPath -Force", control)
        self.assertIn("Remove-Item -LiteralPath $ProbePath -Force", control)
        self.assertNotIn("Remove-Item -Recurse", control)
        self.assertNotIn("rm -rf", control)
        self.assertIn('"health" { Invoke-HealthCheck; Wait-BeforeExit }', control)
        self.assertIn('"repair" { Repair-StaleServerState; Wait-BeforeExit }', control)

    def test_control_script_exposes_backup_and_restore_center_actions(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("function Create-SqliteBackup", control)
        self.assertIn("function Open-BackupsDirectory", control)
        self.assertIn("function Open-RestoreCenter", control)
        self.assertIn("function Verify-LatestBackup", control)
        self.assertIn("function Open-AccountRecovery", control)
        self.assertIn("Create SQLite backup", control)
        self.assertIn("Open backups directory", control)
        self.assertIn("Open restore center", control)
        self.assertIn("Verify latest backup", control)
        self.assertIn("Open account recovery", control)
        self.assertIn("Douyin Recall Backup Now", control)
        self.assertIn("Douyin Recall Backups", control)
        self.assertIn("Douyin Recall Restore Center", control)
        self.assertIn("Douyin Recall Verify Backup", control)
        self.assertIn("Douyin Recall Account Recovery", control)
        self.assertIn("Backups directory:", control)
        self.assertIn("New-Item -ItemType Directory -Path $ExportsDir -Force", control)
        self.assertIn("Start-Process $ExportsDir", control)
        self.assertIn('"backup" { Create-SqliteBackup; Wait-BeforeExit }', control)
        self.assertIn('"backups" { Open-BackupsDirectory }', control)
        self.assertIn('"restore" { Open-RestoreCenter }', control)
        self.assertIn('"verify-backup" { Verify-LatestBackup; Wait-BeforeExit }', control)
        self.assertIn('"auth" { Open-AccountRecovery }', control)
        self.assertIn("-OpenPath \"/maintenance\"", control)
        self.assertNotIn("Remove-Item -Recurse", control)
        self.assertNotIn("rm -rf", control)

    def test_control_script_prints_status_summary_before_menu_actions(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("function Get-InstalledVersion", control)
        self.assertIn("function Read-ServerState", control)
        self.assertIn("function Test-RecordedProcessRunning", control)
        self.assertIn("function Get-ControlSummary", control)
        self.assertIn("function Write-ControlSummary", control)
        self.assertIn("data\\runtime\\server.json", control)
        self.assertIn("ConvertFrom-Json", control)
        self.assertIn('PSObject.Properties["pid"]', control)
        self.assertIn("Get-Process -Id", control)
        self.assertIn("Current version:", control)
        self.assertIn("Service state:", control)
        self.assertIn("Maintenance:", control)
        self.assertIn("Logs:", control)
        self.assertIn("Runtime cache:", control)
        self.assertIn("Stop entry: Douyin Recall Stop Service", control)
        self.assertIn("Start entry: Douyin Recall", control)
        self.assertLess(control.index("Write-ControlSummary"), control.index("Write-Host \"Douyin Recall Control\""))
        self.assertLess(control.index("Write-ControlSummary"), control.index("Invoke-RecallCommand @('status')"))

    def test_inno_installs_start_menu_control_shortcuts(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("control-douyin-recall.ps1", script)
        self.assertIn("Douyin Recall Control", script)
        self.assertIn("Douyin Recall Status", script)
        self.assertIn("Douyin Recall Prepare Runtime", script)
        self.assertIn("Douyin Recall Stop Service", script)
        self.assertIn("Douyin Recall Maintenance", script)
        self.assertIn("Douyin Recall Diagnostics", script)
        self.assertIn("Douyin Recall Logs", script)
        self.assertIn("Douyin Recall Health Check", script)
        self.assertIn("Douyin Recall Repair State", script)
        self.assertIn("Douyin Recall Backup Now", script)
        self.assertIn("Douyin Recall Backups", script)
        self.assertIn("Douyin Recall Restore Center", script)
        self.assertIn("Douyin Recall Verify Backup", script)
        self.assertIn("Douyin Recall Account Recovery", script)
        self.assertIn('-Action ""status""', script)
        self.assertIn('-Action ""prepare""', script)
        self.assertIn('-Action ""stop""', script)
        self.assertIn('-Action ""maintenance""', script)
        self.assertIn('-Action ""diagnose""', script)
        self.assertIn('-Action ""logs""', script)
        self.assertIn('-Action ""health""', script)
        self.assertIn('-Action ""repair""', script)
        self.assertIn('-Action ""backup""', script)
        self.assertIn('-Action ""backups""', script)
        self.assertIn('-Action ""restore""', script)
        self.assertIn('-Action ""verify-backup""', script)
        self.assertIn('-Action ""auth""', script)

    def test_inno_installer_uses_simplified_chinese_ui(self) -> None:
        script = read("DouyinRecall.iss")
        language_file = PACKAGING / "ChineseSimplified.isl"

        self.assertTrue(language_file.exists(), f"missing vendored Inno language file: {language_file}")
        self.assertIn("[Languages]", script)
        self.assertIn('Name: "chinesesimplified"', script)
        self.assertIn('MessagesFile: "ChineseSimplified.isl"', script)
        self.assertNotIn("compiler:Languages\\ChineseSimplified.isl", script)
        self.assertIn('Description: "安装完成后启动 Douyin Recall"', script)
        self.assertIn('Description: "创建桌面快捷方式"', script)

    def test_inno_finish_launch_runs_launcher_silent_and_hidden(self) -> None:
        script = read("DouyinRecall.iss")
        run_entries = [
            line
            for line in script.splitlines()
            if "launch-douyin-recall.vbs" in line and "postinstall" in line
        ]

        self.assertEqual(1, len(run_entries))
        run_entry = run_entries[0].lower()
        self.assertIn("wscript.exe", run_entry)
        self.assertIn("runhidden", run_entry)
        self.assertIn("nowait", run_entry)
        self.assertIn("unchecked", run_entry)
        self.assertIn("skipifsilent", run_entry)

    def test_main_launcher_shortcuts_hide_powershell_window(self) -> None:
        script = read("DouyinRecall.iss")
        launcher_entries = [
            line
            for line in script.splitlines()
            if "start-douyin-recall.ps1" in line and "Name:" in line
        ]

        self.assertEqual(0, len(launcher_entries))

        hidden_launcher_entries = [
            line
            for line in script.splitlines()
            if "launch-douyin-recall.vbs" in line and "Name:" in line
        ]
        self.assertEqual(2, len(hidden_launcher_entries))
        for entry in hidden_launcher_entries:
            self.assertIn('Filename: "{sys}\\wscript.exe"', entry)
            self.assertNotIn("WindowsPowerShell", entry)

    def test_main_launcher_shortcuts_use_app_icon_instead_of_powershell_icon(self) -> None:
        script = read("DouyinRecall.iss")
        icon = PACKAGING / "DouyinRecall.ico"
        launcher_entries = [
            line
            for line in script.splitlines()
            if "launch-douyin-recall.vbs" in line and "Name:" in line
        ]

        self.assertTrue(icon.exists(), f"missing app icon: {icon}")
        self.assertIn("SetupIconFile={#SourceRoot}\\packaging\\windows\\DouyinRecall.ico", script)
        self.assertIn("UninstallDisplayIcon={app}\\packaging\\windows\\DouyinRecall.ico", script)
        self.assertEqual(2, len(launcher_entries))
        for entry in launcher_entries:
            self.assertIn('IconFilename: "{app}\\packaging\\windows\\DouyinRecall.ico"', entry)

    def test_finish_launch_uses_wscript_hidden_launcher_to_avoid_console_flash(self) -> None:
        script = read("DouyinRecall.iss")
        run_entries = [
            line
            for line in script.splitlines()
            if "launch-douyin-recall.vbs" in line and "postinstall" in line
        ]

        self.assertEqual(1, len(run_entries))
        run_entry = run_entries[0].lower()
        self.assertIn("wscript.exe", run_entry)
        self.assertIn("runhidden", run_entry)
        self.assertIn("nowait", run_entry)
        self.assertIn("skipifsilent", run_entry)

    def test_inno_runs_best_effort_preinstall_database_backup(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("[Code]", script)
        self.assertIn("procedure CurStepChanged(CurStep: TSetupStep);", script)
        self.assertIn("CreatePreInstallDatabaseBackup", script)
        self.assertIn("preinstall-backup-douyin-recall.ps1", script)
        self.assertIn("ExtractTemporaryFile('preinstall-backup-douyin-recall.ps1')", script)
        self.assertIn("Exec(ExpandConstant('{sys}\\WindowsPowerShell\\v1.0\\powershell.exe')", script)
        self.assertIn("recall.db", script)
        self.assertIn("data\\exports", script)
        self.assertIn("pre-install-recall-", script)
        self.assertIn("CopyFile(SourceDb, BackupPath, False)", script)
        self.assertIn("Pre-install backup skipped: recall.db not found.", script)
        self.assertIn("Pre-install database backup:", script)
        self.assertNotIn("FileCopy(", script)
        self.assertNotIn("DeleteFile(", script)
        self.assertNotIn("DelTree(", script)

    def test_preinstall_backup_script_uses_sqlite_backup_api_before_copy_fallback(self) -> None:
        script = read("preinstall-backup-douyin-recall.ps1")

        self.assertIn("sqlite3.connect(source_path)", script)
        self.assertIn("source.backup(destination)", script)
        self.assertIn("Copy-Item -LiteralPath $SourceDb -Destination $BackupPath -Force", script)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", script)
        self.assertIn('$env:UV_LINK_MODE = "copy"', script)
        self.assertIn("pre-install-recall-", script)
        self.assertNotIn("Remove-Item -Recurse", script)
        self.assertNotIn("rm -rf", script)

    def test_build_script_requires_inno_setup_and_creates_setup_exe(self) -> None:
        build = read("build-installer.ps1")

        self.assertIn("ISCC.exe", build)
        self.assertIn("DouyinRecall.iss", build)
        self.assertIn("DouyinRecallSetup.exe", build)
        self.assertIn("packaging\\windows\\out", build)

    def test_installed_qa_restores_inno_registration_after_isolated_install(self) -> None:
        script = read_script("qa-installed-build.ps1")

        self.assertIn("$UninstallRegistryPath", script)
        self.assertIn("function Save-InnoRegistration", script)
        self.assertIn("function Restore-InnoRegistration", script)
        self.assertIn("Get-ItemProperty -LiteralPath $UninstallRegistryPath", script)
        self.assertIn("New-ItemProperty", script)
        self.assertIn("Remove-ItemProperty", script)
        self.assertIn("$originalRegistration = Save-InnoRegistration", script)
        self.assertIn("Restore-InnoRegistration -Snapshot $originalRegistration", script)
        self.assertLess(
            script.index("$originalRegistration = Save-InnoRegistration"),
            script.index("Start-Process -FilePath $InstallerPath"),
        )
        self.assertLess(
            script.index("Stop-PortOwner -LocalPort $Port"),
            script.index("Restore-InnoRegistration -Snapshot $originalRegistration"),
        )

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
        self.assertIn("no longer opens that preparation page during normal startup", notes)
        self.assertIn("run the launcher hidden in the background", notes)
        self.assertIn("waits until the local Web endpoint is actually reachable", notes)
        self.assertIn("always shows the installation directory page", notes)
        self.assertIn("控制入口", notes)
        self.assertIn("状态摘要", notes)
        self.assertIn("健康检查", notes)
        self.assertIn("Douyin Recall Health Check", notes)
        self.assertIn("Douyin Recall Repair State", notes)
        self.assertIn("Douyin Recall Prepare Runtime", notes)
        self.assertIn("does not start the local web service", notes)
        self.assertIn("Douyin Recall Backup Now", notes)
        self.assertIn("Douyin Recall Backups", notes)
        self.assertIn("Douyin Recall Restore Center", notes)
        self.assertIn("Douyin Recall Verify Backup", notes)
        self.assertIn("recall verify-backup", notes)
        self.assertIn("Douyin Recall Account Recovery", notes)
        self.assertIn("/auth", notes)
        self.assertIn("pre-install-recall-", notes)
        self.assertIn("data\\exports", notes)
        self.assertIn("Douyin Recall Stop Service", notes)
        self.assertIn("python -m src.cli stop", notes)
        self.assertIn("/maintenance", notes)

    def test_windows_troubleshooting_doc_covers_installer_recovery(self) -> None:
        self.assertTrue(WINDOWS_TROUBLESHOOTING.exists())
        doc = WINDOWS_TROUBLESHOOTING.read_text(encoding="utf-8")

        self.assertIn("Windows 安装包排障", doc)
        self.assertIn("SmartScreen", doc)
        self.assertIn("D:\\codexDownload\\douyinclaude-runtime", doc)
        self.assertIn("start-douyin-recall.log", doc)
        self.assertIn("uv run python -m src.cli status", doc)
        self.assertIn("uv run python -m src.cli stop", doc)
        self.assertIn("uv run python -m src.cli diagnose", doc)
        self.assertIn("启动前健康检查", doc)
        self.assertIn("正常启动不会再自动打开这个准备页", doc)
        self.assertIn("不会显示 PowerShell 进度窗口", doc)
        self.assertIn("只有确认 `http://127.0.0.1:<端口>` 已经可访问后才打开最终页面", doc)
        self.assertIn("Douyin Recall Control", doc)
        self.assertIn("Douyin Recall Stop Service", doc)
        self.assertIn("状态摘要", doc)
        self.assertIn("健康检查", doc)
        self.assertIn("Douyin Recall Health Check", doc)
        self.assertIn("Douyin Recall Repair State", doc)
        self.assertIn("Douyin Recall Prepare Runtime", doc)
        self.assertIn("不会启动本地 Web 服务", doc)
        self.assertIn("Douyin Recall Backup Now", doc)
        self.assertIn("Douyin Recall Backups", doc)
        self.assertIn("Douyin Recall Restore Center", doc)
        self.assertIn("Douyin Recall Verify Backup", doc)
        self.assertIn("uv run python -m src.cli verify-backup", doc)
        self.assertIn("Douyin Recall Account Recovery", doc)
        self.assertIn("/auth", doc)
        self.assertIn("pre-install-recall-", doc)
        self.assertIn("data\\exports", doc)
        self.assertIn("/maintenance", doc)


if __name__ == "__main__":
    unittest.main()

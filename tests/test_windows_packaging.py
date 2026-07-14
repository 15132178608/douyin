from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"
PR_CI_WORKFLOW = ROOT / ".github" / "workflows" / "pr-ci.yml"
RELEASE_NOTES_DIR = ROOT / "docs" / "releases"
WINDOWS_TROUBLESHOOTING = ROOT / "docs" / "windows-troubleshooting.md"
WINDOWS_TEST_ARTIFACTS = Path(r"D:\codexDownload\douyin-installer-progress-retry-tests")
FAKE_RUNTIME_UV = r"""@echo off
setlocal
if not "%FAKE_UV_CALL_LOG%"=="" echo %*>> "%FAKE_UV_CALL_LOG%"
if "%1|%2"=="sync|--check" goto sync_check
if "%1"=="sync" goto sync
if "%1|%2|%3|%4"=="run|playwright|install|chromium" goto browser
if "%1|%2|%3|%4|%5"=="run|playwright|install|--force|chromium" goto browser_force
if "%1|%2|%3|%4|%5"=="run|python|-m|src.cli|init-db" goto database
if "%1|%2|%3|%4|%5"=="run|python|-m|src.cli|status" goto status
echo fake uv received unsupported arguments: %* 1>&2
exit /b 90
:sync_check
if /I "%FAKE_UV_FORCE_SYNC%"=="1" exit /b 1
if exist "%CD%\.venv\Scripts\python.exe" exit /b 0
exit /b 1
:sync
if /I "%FAKE_UV_FAIL_STAGE%"=="python" (
  echo simulated Python dependency failure 1>&2
  exit /b 41
)
if /I not "%FAKE_UV_BLOCK_STAGE%"=="python" goto sync_after_block
echo started> "%CD%\fake-uv-child-started.txt"
:wait_sync_release
if exist "%CD%\fake-uv-child-release.txt" goto sync_released
powershell.exe -NoProfile -Command "Start-Sleep -Milliseconds 100"
goto wait_sync_release
:sync_released
echo completed> "%CD%\fake-uv-child-completed.txt"
:sync_after_block
if not exist "%CD%\.venv\Scripts" mkdir "%CD%\.venv\Scripts"
if not exist "%CD%\.venv\Lib\site-packages\playwright\driver\package" mkdir "%CD%\.venv\Lib\site-packages\playwright\driver\package"
echo fixture> "%CD%\.venv\Scripts\python.exe"
> "%CD%\.venv\Lib\site-packages\playwright\driver\package\browsers.json" echo {"browsers":[{"name":"chromium","revision":"fixture"},{"name":"chromium-headless-shell","revision":"fixture"},{"name":"ffmpeg","revision":"fixture"},{"name":"winldd","revision":"fixture"}]}
echo Resolved and installed fake Python dependencies
exit /b 0
:browser
set "FAKE_BROWSER_FORCE=0"
goto browser_common
:browser_force
set "FAKE_BROWSER_FORCE=1"
:browser_common
if /I "%FAKE_UV_FAIL_STAGE%"=="browser" (
  echo simulated Playwright Chromium failure 1>&2
  exit /b 42
)
if not exist "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\chrome-win64" mkdir "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\chrome-win64"
if not exist "%PLAYWRIGHT_BROWSERS_PATH%\chromium_headless_shell-fixture\chrome-headless-shell-win64" mkdir "%PLAYWRIGHT_BROWSERS_PATH%\chromium_headless_shell-fixture\chrome-headless-shell-win64"
if not exist "%PLAYWRIGHT_BROWSERS_PATH%\ffmpeg-fixture" mkdir "%PLAYWRIGHT_BROWSERS_PATH%\ffmpeg-fixture"
if not exist "%PLAYWRIGHT_BROWSERS_PATH%\winldd-fixture" mkdir "%PLAYWRIGHT_BROWSERS_PATH%\winldd-fixture"
if /I "%FAKE_UV_BROWSER_ALWAYS_INCOMPLETE%"=="1" (
  echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\chrome-win64\chrome.exe"
  echo Simulated incomplete browser install with exit code 0
  exit /b 0
)
if /I "%FAKE_UV_LEAVE_BROWSER_INCOMPLETE_ONCE%|%FAKE_BROWSER_FORCE%"=="1|0" if not exist "%CD%\fake-browser-incomplete-once.txt" (
  echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\chrome-win64\chrome.exe"
  echo incomplete> "%CD%\fake-browser-incomplete-once.txt"
  echo Simulated incomplete browser install with exit code 0
  exit /b 0
)
echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\chrome-win64\chrome.exe"
echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\chromium_headless_shell-fixture\chrome-headless-shell-win64\chrome-headless-shell.exe"
echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\ffmpeg-fixture\ffmpeg-win64.exe"
echo fixture> "%PLAYWRIGHT_BROWSERS_PATH%\winldd-fixture\PrintDeps.exe"
echo complete> "%PLAYWRIGHT_BROWSERS_PATH%\chromium-fixture\INSTALLATION_COMPLETE"
echo complete> "%PLAYWRIGHT_BROWSERS_PATH%\chromium_headless_shell-fixture\INSTALLATION_COMPLETE"
echo complete> "%PLAYWRIGHT_BROWSERS_PATH%\ffmpeg-fixture\INSTALLATION_COMPLETE"
echo complete> "%PLAYWRIGHT_BROWSERS_PATH%\winldd-fixture\INSTALLATION_COMPLETE"
echo Downloading Chromium 10%% of 100 MiB
echo Chromium download complete
exit /b 0
:database
if /I "%FAKE_UV_FAIL_STAGE%"=="database" (
  echo simulated database failure 1>&2
  exit /b 43
)
if not exist "%CD%\data" mkdir "%CD%\data"
echo fixture> "%CD%\data\recall.db"
echo Initialized fake database
exit /b 0
:status
if /I "%FAKE_UV_FAIL_STAGE%"=="status" (
  echo simulated status failure 1>&2
  exit /b 44
)
echo Local web service is stopped
exit /b 0
"""
RELEASE_TOOL_MODULES = [
    "installed_smoke",
    "release_gate",
    "acceptance_matrix",
    "delivery_evidence",
    "final_release_check",
    "preflight_summary",
    "query_performance",
    "performance_benchmark",
    "backup_drill",
    "database_safety",
]


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


def create_runtime_preparation_fixture() -> tuple[Path, dict[str, str]]:
    case = WINDOWS_TEST_ARTIFACTS / f"case-路径-{uuid.uuid4().hex}"
    windows_dir = case / "packaging" / "windows"
    windows_dir.mkdir(parents=True)
    runtime_root = WINDOWS_TEST_ARTIFACTS / f"runtime-cache-{uuid.uuid4().hex}"

    control = read("control-douyin-recall.ps1").replace(
        r"D:\codexDownload\douyinclaude-runtime",
        str(runtime_root),
    )
    (windows_dir / "control-douyin-recall.ps1").write_text(control, encoding="ascii")

    launcher = read("start-douyin-recall.ps1").lstrip("\ufeff").replace(
        r"D:\codexDownload\douyinclaude-runtime",
        str(runtime_root),
    )
    (windows_dir / "start-douyin-recall.ps1").write_text(
        launcher,
        encoding="utf-8-sig",
    )
    for helper_name in (
        "runtime-preparation-common.ps1",
        "runtime-tool-runner.ps1",
        "runtime-tool-worker.ps1",
    ):
        shutil.copy2(PACKAGING / helper_name, windows_dir)
    fake_uv_cmd = case / "fake-runtime-uv.cmd"
    fake_uv_cmd.write_text(FAKE_RUNTIME_UV, encoding="ascii")

    (case / ".env.example").write_text("WEB_PORT=18765\n", encoding="utf-8")
    (case / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (case / "uv.lock").write_text("fixture-lock\n", encoding="utf-8")
    call_log = case / "fake-uv-calls.log"
    env = os.environ.copy()
    env.update(
        {
            "UV_EXE": str(fake_uv_cmd),
            "FAKE_UV_CALL_LOG": str(call_log),
            "FAKE_UV_FAIL_STAGE": "",
            "FAKE_UV_BLOCK_STAGE": "",
            "FAKE_UV_FORCE_SYNC": "",
            "FAKE_UV_LEAVE_BROWSER_INCOMPLETE_ONCE": "",
            "FAKE_UV_BROWSER_ALWAYS_INCOMPLETE": "",
            "FAKE_RUNTIME_ROOT": str(runtime_root),
        }
    )
    return case, env


def run_powershell(script: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=script.parents[2],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )


def wait_for_test_path(path: Path, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"helper process exited before creating {path}: {process.returncode}")
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def run_powershell_command(command: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
        check=False,
    )


def find_runtime_runner_identity(
    owner_pid: int, *, cwd: Path, timeout: float = 15.0
) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    command = (
        f'$runner = Get-CimInstance Win32_Process -Filter "ParentProcessId = {owner_pid}" | '
        "Where-Object { $_.Name -ieq 'powershell.exe' -and "
        "$_.CommandLine -like '*runtime-tool-runner.ps1*' } | "
        "Select-Object -First 1; "
        "if ($null -eq $runner) { exit 1 }; "
        "$process = Get-Process -Id $runner.ProcessId -ErrorAction Stop; "
        "Write-Output \"$($process.Id)|$($process.StartTime.ToUniversalTime().Ticks)\""
    )
    while time.monotonic() < deadline:
        result = run_powershell_command(command, cwd=cwd)
        parts = result.stdout.strip().split("|")
        if result.returncode == 0 and len(parts) == 2 and all(part.isdigit() for part in parts):
            return int(parts[0]), int(parts[1])
        time.sleep(0.05)
    raise AssertionError(f"timed out finding runtime runner for owner pid {owner_pid}")


def find_child_process_identity(
    owner_pid: int, *, cwd: Path, timeout: float = 15.0
) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    command = (
        f'$child = Get-CimInstance Win32_Process -Filter "ParentProcessId = {owner_pid}" | '
        "Where-Object { $_.Name -ieq 'cmd.exe' } | Select-Object -First 1; "
        "if ($null -eq $child) { exit 1 }; "
        "$process = Get-Process -Id $child.ProcessId -ErrorAction Stop; "
        "Write-Output \"$($process.Id)|$($process.StartTime.ToUniversalTime().Ticks)\""
    )
    while time.monotonic() < deadline:
        result = run_powershell_command(command, cwd=cwd)
        parts = result.stdout.strip().split("|")
        if result.returncode == 0 and len(parts) == 2 and all(part.isdigit() for part in parts):
            return int(parts[0]), int(parts[1])
        time.sleep(0.05)
    raise AssertionError(f"timed out finding child process for owner pid {owner_pid}")


def process_identity_exists(process_id: int, started_at_ticks: int, *, cwd: Path) -> bool:
    result = run_powershell_command(
        f"$process = Get-Process -Id {process_id} -ErrorAction SilentlyContinue; "
        "if ($null -eq $process) { exit 1 }; "
        f"if ($process.StartTime.ToUniversalTime().Ticks -eq {started_at_ticks}) "
        "{ exit 0 } else { exit 1 }",
        cwd=cwd,
    )
    return result.returncode == 0


def stop_process_if_identity_matches(
    process_id: int, started_at_ticks: int, *, cwd: Path
) -> None:
    result = run_powershell_command(
        f"$process = Get-Process -Id {process_id} -ErrorAction SilentlyContinue; "
        "if ($null -eq $process) { exit 0 }; "
        f"if ($process.StartTime.ToUniversalTime().Ticks -ne {started_at_ticks}) {{ exit 0 }}; "
        "Stop-Process -InputObject $process -Force -ErrorAction Stop",
        cwd=cwd,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


def wait_for_process_exit(
    process_id: int,
    *,
    cwd: Path,
    started_at_ticks: int,
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_identity_exists(process_id, started_at_ticks, cwd=cwd):
            return
        time.sleep(0.1)
    raise AssertionError(f"process {process_id} remained alive")


class WindowsPackagingTests(unittest.TestCase):
    def test_inno_installer_uses_per_user_install_and_excludes_private_data(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\DouyinRecall", script)
        self.assertIn("data\\*", script)
        self.assertIn("dist\\*", script)
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
        self.assertIn("$env:HF_HOME", launcher)
        self.assertIn("$env:SENTENCE_TRANSFORMERS_HOME", launcher)
        self.assertIn("$HuggingFaceCacheDir", launcher)
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

    def test_launcher_only_opens_progress_page_for_unprepared_or_failed_startup(self) -> None:
        launcher = read("start-douyin-recall.ps1")
        runner = read("runtime-tool-runner.ps1")
        worker = read("runtime-tool-worker.ps1")

        self.assertIn("$StartupStatusPath", launcher)
        self.assertIn("function Write-StartupStatusPage", launcher)
        self.assertIn("function Update-StartupStatus", launcher)
        self.assertIn("function Show-StartupStatusPage", launcher)
        self.assertIn("param([string]$Path = $StartupStatusPath)", launcher)
        self.assertIn("Start-Process $Path", launcher)
        self.assertIn("$StartupWaitStatusPath", launcher)
        self.assertIn("[switch]$NoOpen", launcher)
        self.assertIn("First-run progress page will redirect", launcher)
        self.assertIn("-RedirectUrl $redirectUrl", launcher)
        self.assertLess(
            launcher.index("Enter-RecallPreparationLock -Path $PreparationLockPath"),
            launcher.index("if (Test-RuntimePrepared)"),
        )
        self.assertLess(
            launcher.index("Enter-RecallPreparationLock -Path $PreparationLockPath"),
            launcher.index("Show-StartupStatusPage", launcher.index("try {\n    Set-Location")),
        )
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
        self.assertIn("Get-LatestToolOutput", launcher)
        self.assertIn("Invoke-StartupTool", launcher)
        self.assertIn("最新输出", launcher)
        self.assertIn("Could not refresh startup progress", launcher)
        self.assertIn("$process.Kill()", launcher)
        self.assertIn("runtime-$Key.child.json", launcher)
        self.assertIn("owner_started_at_ticks", launcher)
        self.assertIn("started_at_ticks", launcher)
        self.assertIn("$runnerProtocolCompleted = $true", launcher)
        self.assertNotIn("$runnerCompleted = ($exitCode -ge 0)", launcher)
        self.assertIn("runtime-$Key.runner.json", launcher)
        self.assertIn("runtime-$Key.worker.json", launcher)
        self.assertIn("$RuntimeToolRunnerScript", launcher)
        self.assertIn("$RuntimeToolWorkerScript", launcher)
        self.assertNotIn("-EncodedCommand", launcher)
        self.assertIn("function Test-RunnerOwnerAlive", runner)
        self.assertIn("actualStartedAtTicks -eq $expectedStartedAtTicks", runner)
        self.assertNotIn("TotalSeconds", runner)
        self.assertNotIn("taskkill", runner.lower())
        self.assertIn("worker_script_path", runner)
        self.assertIn("-SpecPath", runner)
        self.assertNotIn("-EncodedCommand", runner)
        self.assertIn("tool_exit_code_path", worker)
        self.assertNotIn("taskkill", launcher.lower())
        self.assertIn("finally {", launcher)
        self.assertIn("$script:OwnsPreparationLock", launcher)
        self.assertIn("$StartupFailureStatusPath", launcher)
        self.assertIn("Browser opening suppressed by -NoOpen", launcher)

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
        common = read("runtime-preparation-common.ps1")

        self.assertIn("$RuntimePreparedPath", launcher)
        self.assertIn("$VenvPython", launcher)
        self.assertIn("[System.Security.Cryptography.SHA256]::Create()", common)
        self.assertNotIn("Get-FileHash", common)
        self.assertIn("function Test-RuntimePrepared", launcher)
        self.assertIn("function Write-RuntimePreparedMarker", launcher)
        self.assertIn("function Start-RecallServiceProcess", launcher)
        self.assertIn("运行环境已准备，跳过 uv sync 和 Playwright 安装", launcher)
        self.assertIn("Invalidated the previous runtime-prepared marker before full preparation", launcher)
        self.assertIn('install", "--force", "chromium"', launcher)
        self.assertIn("failed exact post-install validation", launcher)
        self.assertIn("function Stop-StaleDouyinRecallServiceOnPort", launcher)
        self.assertIn("$LauncherPath", launcher)
        self.assertIn("LastWriteTimeUtc", launcher)
        self.assertIn("started_at", launcher)
        self.assertIn("Recorded Douyin Recall service predates this launcher", launcher)
        self.assertIn("Stop-StaleDouyinRecallServiceOnPort -Port $port", launcher)
        self.assertIn("Stop-Process -Id $owner.ProcessId -Force", launcher)
        self.assertIn("Douyin Recall is already running; opening browser without runtime preparation", launcher)
        self.assertIn("Start-RecallServiceProcess -UsePreparedRuntime", launcher)
        self.assertNotIn("falling back to full preparation", launcher)
        self.assertLess(
            launcher.index("if (Test-WebReady -Url $url -TimeoutSec 1)"),
            launcher.index("Stop-StaleDouyinRecallServiceOnPort -Port $port"),
        )
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

        self.assertIn('[ValidateSet("menu", "start", "prepare", "stop", "status", "maintenance", "auth", "diagnose", "logs", "update", "health", "repair", "backup", "backups", "restore", "verify-backup", "rollback-check")]', control)
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
        self.assertIn("Invoke-RecallCommand @('rollback-from-manifest', '--manifest', $ManifestPath, '--json')", control)
        self.assertNotIn("'rollback-from-manifest', '--apply'", control)
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
        self.assertIn("[switch]$NonInteractive", control)
        self.assertIn("DR_PROGRESS|$Event|", control)
        self.assertIn("Write-RecallRuntimePreparedMarker", control)
        self.assertIn("Test-RecallPlaywrightChromiumReady", control)
        self.assertIn("playwright install --force chromium", control)
        self.assertIn("failed exact post-install validation", control)
        self.assertIn("$script:CompletedPrepareSteps", control)
        self.assertIn("runtime-preparation.lock", control)
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

    def test_runtime_preparation_common_supports_current_chromium_and_strict_marker(self) -> None:
        common = read("runtime-preparation-common.ps1")

        self.assertIn('"chrome-win\\chrome.exe"', common)
        self.assertIn('"chrome-win64\\chrome.exe"', common)
        self.assertIn('"chrome-headless-shell-win64\\chrome-headless-shell.exe"', common)
        self.assertIn('"ffmpeg-win64.exe"', common)
        self.assertIn('"PrintDeps.exe"', common)
        self.assertIn('"INSTALLATION_COMPLETE"', common)
        self.assertIn("-PathType Leaf", common)
        self.assertIn("PlaywrightBrowsersJsonPath", common)
        self.assertIn('"chromium-$revision"', common)
        self.assertIn("function Test-RecallRuntimePrepared", common)
        self.assertIn("if (-not (Test-Path -LiteralPath $RuntimePreparedPath))", common)
        self.assertIn("schema_version", common)
        self.assertIn("Get-RecallRuntimeFingerprint", common)
        self.assertIn("Runtime fingerprint input is missing", common)
        self.assertIn("[System.IO.File]::Replace", common)
        self.assertIn("[System.IO.FileShare]::None", common)

    def test_inno_installer_shows_fresh_runtime_progress_with_retry_or_defer(self) -> None:
        script = read("DouyinRecall.iss")

        self.assertIn("#if Ver < EncodeVer(6, 5, 0)", script)
        self.assertIn('Name: "prepareruntime"', script)
        self.assertIn("ShouldOfferRuntimePreparationTask", script)
        self.assertIn("CreateOutputProgressPage", script)
        self.assertIn("ExecAndLogOutput", script)
        self.assertIn("GetExceptionMessage", script)
        self.assertIn("EventName = 'BUSY'", script)
        self.assertIn('\'" -Action "prepare" -NonInteractive\'', script)
        self.assertIn("DR_PROGRESS|", script)
        self.assertIn("MB_RETRYCANCEL", script)
        self.assertIn("RuntimePreparationDeferred", script)
        self.assertIn("ShouldLaunchAfterInstall", script)
        self.assertIn("CurStep = ssPostInstall", script)
        self.assertIn("not WizardSilent()", script)
        self.assertIn("not RuntimePreparationIsUpgrade", script)
        self.assertIn("WizardIsTaskSelected('prepareruntime')", script)
        self.assertLess(
            script.index("CurStep = ssInstall"),
            script.index("CurStep = ssPostInstall"),
        )

    @unittest.skipUnless(os.name == "nt", "runtime preparation harness requires Windows")
    def test_prepare_runtime_failure_retries_only_unready_stage(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"

        env["FAKE_UV_FAIL_STAGE"] = "browser"
        failed = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("DR_PROGRESS|FAILED|3|5|browser", failed.stdout)

        env["FAKE_UV_FAIL_STAGE"] = ""
        retried = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("DR_PROGRESS|SKIP|2|5|python", retried.stdout)
        self.assertIn("DR_PROGRESS|COMPLETE|5|5|complete", retried.stdout)

        calls = (case / "fake-uv-calls.log").read_text(encoding="utf-8")
        self.assertEqual(calls.count("sync --no-dev --color never"), 1)
        self.assertEqual(calls.count("run playwright install chromium"), 2)
        marker = json.loads(
            (case / "data" / "runtime" / "runtime-prepared.json").read_text(
                encoding="utf-8-sig"
            )
        )
        state = json.loads(
            (case / "data" / "runtime" / "runtime-preparation.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(marker["schema_version"], 1)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(
            state["completed_steps"],
            ["uv", "python", "browser", "database", "status"],
        )

    @unittest.skipUnless(os.name == "nt", "runtime preparation harness requires Windows")
    def test_prepare_runtime_force_repairs_incomplete_browser_install(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"
        env["FAKE_UV_LEAVE_BROWSER_INCOMPLETE_ONCE"] = "1"

        result = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = (case / "fake-uv-calls.log").read_text(encoding="utf-8")
        self.assertIn("run playwright install chromium", calls)
        self.assertIn("run playwright install --force chromium", calls)
        self.assertTrue((case / "data" / "runtime" / "runtime-prepared.json").exists())

    @unittest.skipUnless(os.name == "nt", "runtime preparation harness requires Windows")
    def test_prepare_runtime_does_not_report_browser_done_before_failed_postcondition(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"
        env["FAKE_UV_BROWSER_ALWAYS_INCOMPLETE"] = "1"

        result = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DR_PROGRESS|FAILED|3|5|browser", result.stdout)
        self.assertNotIn("DR_PROGRESS|DONE|3|5|browser", result.stdout)
        state = json.loads(
            (case / "data" / "runtime" / "runtime-preparation.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertNotIn("browser", state["completed_steps"])
        self.assertFalse((case / "data" / "runtime" / "runtime-prepared.json").exists())

    @unittest.skipUnless(os.name == "nt", "runtime marker invalidation harness requires Windows")
    def test_launcher_invalidates_old_marker_before_repairing_missing_component(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        prepared = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)

        marker_path = case / "data" / "runtime" / "runtime-prepared.json"
        chromium_exe = (
            Path(env["FAKE_RUNTIME_ROOT"])
            / "ms-playwright"
            / "chromium-fixture"
            / "chrome-win64"
            / "chrome.exe"
        )
        self.assertTrue(marker_path.exists())
        chromium_exe.unlink()
        env["FAKE_UV_FAIL_STAGE"] = "database"

        failed = run_powershell(launcher, "-Silent", "-NoOpen", env=env)

        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertFalse(marker_path.exists())
        state = json.loads(
            (case / "data" / "runtime" / "runtime-preparation.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["current_step"], "database")

    @unittest.skipUnless(os.name == "nt", "Playwright readiness probe requires Windows")
    def test_browser_readiness_rejects_stale_revision_and_missing_headless(self) -> None:
        case, env = create_runtime_preparation_fixture()
        windows_dir = case / "packaging" / "windows"
        browser_cache = case / "browser-cache"
        manifest_path = (
            case
            / ".venv"
            / "Lib"
            / "site-packages"
            / "playwright"
            / "driver"
            / "package"
            / "browsers.json"
        )
        manifest_path.parent.mkdir(parents=True)

        def write_manifest(headless_revision: str = "current") -> None:
            manifest_path.write_text(
                json.dumps(
                    {
                        "browsers": [
                            {"name": "chromium", "revision": "current"},
                            {"name": "chromium-headless-shell", "revision": headless_revision},
                            {"name": "ffmpeg", "revision": "current"},
                            {"name": "winldd", "revision": "current"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

        def create_components(revision: str, *, include_chromium: bool = True) -> None:
            components = [
                (
                    browser_cache / f"chromium_headless_shell-{revision}",
                    Path("chrome-headless-shell-win64") / "chrome-headless-shell.exe",
                ),
                (browser_cache / f"ffmpeg-{revision}", Path("ffmpeg-win64.exe")),
                (browser_cache / f"winldd-{revision}", Path("PrintDeps.exe")),
            ]
            if include_chromium:
                components.append(
                    (browser_cache / f"chromium-{revision}", Path("chrome-win64") / "chrome.exe")
                )
            for browser_dir, relative_path in components:
                path = browser_dir / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="ascii")
                (browser_dir / "INSTALLATION_COMPLETE").write_text(
                    "complete", encoding="ascii"
                )

        probe = windows_dir / "probe-browser-readiness.ps1"
        probe.write_text(
            r'''param([string]$BrowserCache, [string]$ManifestPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
if (Test-RecallPlaywrightChromiumReady -PlaywrightBrowsersDir $BrowserCache -PlaywrightBrowsersJsonPath $ManifestPath) {
    exit 0
}
exit 1
''',
            encoding="utf-8-sig",
        )

        write_manifest()
        create_components("stale")
        create_components("current", include_chromium=False)
        stale_only = run_powershell(probe, str(browser_cache), str(manifest_path), env=env)
        self.assertEqual(stale_only.returncode, 1, stale_only.stdout + stale_only.stderr)

        current_chromium = browser_cache / "chromium-current" / "chrome-win64" / "chrome.exe"
        current_chromium.parent.mkdir(parents=True)
        current_chromium.write_text("fixture", encoding="ascii")
        incomplete = run_powershell(probe, str(browser_cache), str(manifest_path), env=env)
        self.assertEqual(incomplete.returncode, 1, incomplete.stdout + incomplete.stderr)
        (browser_cache / "chromium-current" / "INSTALLATION_COMPLETE").write_text(
            "complete", encoding="ascii"
        )
        ready = run_powershell(probe, str(browser_cache), str(manifest_path), env=env)
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)

        write_manifest(headless_revision="missing-headless")
        missing_headless = run_powershell(probe, str(browser_cache), str(manifest_path), env=env)
        self.assertEqual(missing_headless.returncode, 1, missing_headless.stdout + missing_headless.stderr)

    @unittest.skipUnless(os.name == "nt", "runtime preparation lock harness requires Windows")
    def test_prepare_owner_blocks_launcher_and_second_prepare_without_state_corruption(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"

        prepared = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        state_path = case / "data" / "runtime" / "runtime-preparation.json"
        marker_path = case / "data" / "runtime" / "runtime-prepared.json"
        self.assertTrue(marker_path.exists())
        owner_env = env.copy()
        owner_env["FAKE_UV_FORCE_SYNC"] = "1"
        owner_env["FAKE_UV_BLOCK_STAGE"] = "python"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        owner_stdout = (case / "owner-prepare-stdout.log").open("wb")
        owner_stderr = (case / "owner-prepare-stderr.log").open("wb")
        owner_process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(control),
                "-Action",
                "prepare",
                "-NonInteractive",
            ],
            cwd=case,
            env=owner_env,
            stdout=owner_stdout,
            stderr=owner_stderr,
        )
        try:
            wait_for_test_path(child_started, owner_process)
            self.assertFalse(marker_path.exists())
            owner_state_bytes = state_path.read_bytes()
            owner_state = json.loads(owner_state_bytes.decode("utf-8-sig"))
            self.assertEqual(owner_state["status"], "running")
            self.assertEqual(owner_state["current_step"], "python")
            stale_page = case / "data" / "runtime" / "startup-status.html"
            stale_page.write_text(
                "<meta http-equiv='refresh' content='0;url=http://stale.invalid'>STALE_SUCCESS_SENTINEL",
                encoding="utf-8",
            )

            followed = run_powershell(launcher, "-Silent", "-NoOpen", env=env)
            self.assertEqual(followed.returncode, 0, followed.stdout + followed.stderr)
            self.assertEqual(stale_page.read_text(encoding="utf-8"), "<meta http-equiv='refresh' content='0;url=http://stale.invalid'>STALE_SUCCESS_SENTINEL")
            waiting_page = case / "data" / "runtime" / "startup-status-waiting.html"
            waiting_html = waiting_page.read_text(encoding="utf-8-sig")
            self.assertIn("已有运行环境准备任务正在进行", waiting_html)
            self.assertNotIn("stale.invalid", waiting_html)
            self.assertEqual(state_path.read_bytes(), owner_state_bytes)
            start_log = (case / "data" / "logs" / "start-douyin-recall.log").read_text(
                encoding="utf-8-sig"
            )
            suppressed_lines = [
                line for line in start_log.splitlines() if "opening suppressed by -NoOpen" in line
            ]
            self.assertTrue(suppressed_lines)
            self.assertTrue(
                all("startup-status-waiting.html" in line for line in suppressed_lines)
            )
            self.assertFalse(marker_path.exists())

            busy = run_powershell(control, "-Action", "prepare", "-NonInteractive", env=env)
            self.assertEqual(busy.returncode, 1, busy.stdout + busy.stderr)
            self.assertIn("DR_PROGRESS|BUSY|", busy.stdout)
            self.assertEqual(state_path.read_bytes(), owner_state_bytes)
            calls = (case / "fake-uv-calls.log").read_text(encoding="utf-8")
            self.assertEqual(calls.count("sync --no-dev --color never"), 2)

            child_release.write_text("release", encoding="ascii")
            owner_process.wait(timeout=15)
            self.assertEqual(owner_process.returncode, 0)
            self.assertTrue(marker_path.exists())
            final_state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(final_state["status"], "ready")
            self.assertEqual(
                final_state["completed_steps"],
                ["uv", "python", "browser", "database", "status"],
            )
        finally:
            child_release.write_text("release", encoding="ascii")
            if owner_process.poll() is None:
                try:
                    owner_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    owner_process.terminate()
                    owner_process.wait(timeout=5)
            owner_stdout.close()
            owner_stderr.close()

    @unittest.skipUnless(os.name == "nt", "startup process lifecycle harness requires Windows")
    def test_status_write_failure_keeps_child_and_preparation_lock_owned(self) -> None:
        case, env = create_runtime_preparation_fixture()
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        windows_dir = case / "packaging" / "windows"
        env["FAKE_UV_BLOCK_STAGE"] = "python"
        env["FAKE_UV_FAIL_STAGE"] = "browser"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        child_completed = case / "fake-uv-child-completed.txt"
        status_path = case / "data" / "runtime" / "startup-status.html"
        status_locked = case / "status-locked.txt"
        status_release = case / "status-release.txt"
        lock_path = case / "data" / "runtime" / "runtime-preparation.lock"

        status_locker = windows_dir / "hold-startup-status.ps1"
        status_locker.write_text(
            r'''param([string]$StatusPath, [string]$LockedPath, [string]$ReleasePath)
$ErrorActionPreference = "Stop"
$stream = $null
while ($null -eq $stream) {
    try {
        $stream = [System.IO.File]::Open($StatusPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    }
    catch [System.IO.IOException] {
        Start-Sleep -Milliseconds 50
    }
}
try {
    Set-Content -LiteralPath $LockedPath -Value "locked" -Encoding UTF8
    while (-not (Test-Path -LiteralPath $ReleasePath)) {
        Start-Sleep -Milliseconds 100
    }
}
finally {
    $stream.Dispose()
}
''',
            encoding="utf-8-sig",
        )
        lock_probe = windows_dir / "probe-runtime-lock.ps1"
        lock_probe.write_text(
            r'''param([string]$LockPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
$stream = Enter-RecallPreparationLock -Path $LockPath
if ($null -eq $stream) { exit 1 }
Exit-RecallPreparationLock -LockStream $stream
exit 0
''',
            encoding="utf-8-sig",
        )

        stdout_path = case / "launcher-stdout.log"
        stderr_path = case / "launcher-stderr.log"
        launcher_process: subprocess.Popen[bytes] | None = None
        locker_process: subprocess.Popen[bytes] | None = None
        stdout_handle = stdout_path.open("wb")
        stderr_handle = stderr_path.open("wb")
        try:
            launcher_process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher),
                    "-Silent",
                    "-NoOpen",
                ],
                cwd=case,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            wait_for_test_path(child_started, launcher_process)
            locker_process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(status_locker),
                    str(status_path),
                    str(status_locked),
                    str(status_release),
                ],
                cwd=case,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_for_test_path(status_locked, locker_process)
            start_log_path = case / "data" / "logs" / "start-douyin-recall.log"
            deadline = time.monotonic() + 8
            saw_refresh_failure = False
            while time.monotonic() < deadline:
                if start_log_path.exists():
                    start_log_text = start_log_path.read_text(
                        encoding="utf-8-sig", errors="replace"
                    )
                    if "Could not refresh startup progress" in start_log_text:
                        saw_refresh_failure = True
                        break
                self.assertIsNone(launcher_process.poll())
                time.sleep(0.1)
            self.assertTrue(saw_refresh_failure)
            self.assertIsNone(launcher_process.poll())
            self.assertFalse(child_completed.exists())
            locked_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(locked_probe.returncode, 1, locked_probe.stdout + locked_probe.stderr)

            status_release.write_text("release", encoding="ascii")
            locker_process.wait(timeout=5)
            child_release.write_text("release", encoding="ascii")
            launcher_process.wait(timeout=20)
            self.assertTrue(child_completed.exists())
            self.assertEqual(launcher_process.returncode, 1)
            released_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(released_probe.returncode, 0, released_probe.stdout + released_probe.stderr)
            start_log = start_log_path.read_text(encoding="utf-8-sig")
            self.assertIn("Could not refresh startup progress", start_log)
        finally:
            status_release.write_text("release", encoding="ascii")
            child_release.write_text("release", encoding="ascii")
            if locker_process is not None and locker_process.poll() is None:
                try:
                    locker_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    locker_process.terminate()
                    locker_process.wait(timeout=5)
            if launcher_process is not None and launcher_process.poll() is None:
                try:
                    launcher_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    launcher_process.terminate()
                    launcher_process.wait(timeout=5)
            stdout_handle.close()
            stderr_handle.close()

    @unittest.skipUnless(os.name == "nt", "runner crash harness requires Windows")
    def test_runner_crash_cleans_recorded_child_before_unlocking(self) -> None:
        case, env = create_runtime_preparation_fixture()
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        windows_dir = case / "packaging" / "windows"
        env["FAKE_UV_BLOCK_STAGE"] = "python"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        child_identity_path = case / "data" / "logs" / "runtime-python.child.json"
        lock_path = case / "data" / "runtime" / "runtime-preparation.lock"
        lock_probe = windows_dir / "probe-runtime-lock-after-runner-crash.ps1"
        lock_probe.write_text(
            r'''param([string]$LockPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
$stream = Enter-RecallPreparationLock -Path $LockPath
if ($null -eq $stream) { exit 1 }
Exit-RecallPreparationLock -LockStream $stream
exit 0
''',
            encoding="utf-8-sig",
        )
        stdout_handle = (case / "runner-crash-launcher.out.log").open("wb")
        stderr_handle = (case / "runner-crash-launcher.err.log").open("wb")
        launcher_process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Silent",
                "-NoOpen",
            ],
            cwd=case,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
        child_process_id = 0
        child_started_at_ticks = 0
        runner_process_id = 0
        runner_started_at_ticks = 0
        try:
            wait_for_test_path(child_started, launcher_process)
            wait_for_test_path(child_identity_path, launcher_process)
            child_identity = json.loads(child_identity_path.read_text(encoding="utf-8-sig"))
            child_process_id = int(child_identity["pid"])
            child_started_at_ticks = int(child_identity["started_at_ticks"])
            runner_process_id, runner_started_at_ticks = find_runtime_runner_identity(
                launcher_process.pid, cwd=case
            )
            self.assertTrue(
                process_identity_exists(child_process_id, child_started_at_ticks, cwd=case)
            )

            stop_process_if_identity_matches(
                runner_process_id, runner_started_at_ticks, cwd=case
            )
            launcher_process.wait(timeout=20)

            self.assertEqual(launcher_process.returncode, 1)
            wait_for_process_exit(
                child_process_id, cwd=case, started_at_ticks=child_started_at_ticks
            )
            released_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(released_probe.returncode, 0, released_probe.stdout + released_probe.stderr)
            start_log = (case / "data" / "logs" / "start-douyin-recall.log").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("Cleaning runtime process tree", start_log)
        finally:
            child_release.write_text("release", encoding="ascii")
            if launcher_process.poll() is None:
                launcher_process.terminate()
                launcher_process.wait(timeout=5)
            for process_id, started_at_ticks in (
                (runner_process_id, runner_started_at_ticks),
                (child_process_id, child_started_at_ticks),
            ):
                if process_id and started_at_ticks:
                    stop_process_if_identity_matches(process_id, started_at_ticks, cwd=case)
            stdout_handle.close()
            stderr_handle.close()

    @unittest.skipUnless(os.name == "nt", "worker crash harness requires Windows")
    def test_worker_crash_cleans_tool_process_before_unlocking(self) -> None:
        case, env = create_runtime_preparation_fixture()
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        windows_dir = case / "packaging" / "windows"
        env["FAKE_UV_BLOCK_STAGE"] = "python"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        child_completed = case / "fake-uv-child-completed.txt"
        child_identity_path = case / "data" / "logs" / "runtime-python.child.json"
        lock_path = case / "data" / "runtime" / "runtime-preparation.lock"
        lock_probe = windows_dir / "probe-runtime-lock-after-worker-crash.ps1"
        lock_probe.write_text(
            r'''param([string]$LockPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
$stream = Enter-RecallPreparationLock -Path $LockPath
if ($null -eq $stream) { exit 1 }
Exit-RecallPreparationLock -LockStream $stream
exit 0
''',
            encoding="utf-8-sig",
        )
        stdout_handle = (case / "worker-crash-launcher.out.log").open("wb")
        stderr_handle = (case / "worker-crash-launcher.err.log").open("wb")
        launcher_process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Silent",
                "-NoOpen",
            ],
            cwd=case,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
        worker_process_id = 0
        worker_started_at_ticks = 0
        tool_process_id = 0
        tool_started_at_ticks = 0
        try:
            wait_for_test_path(child_started, launcher_process)
            wait_for_test_path(child_identity_path, launcher_process)
            child_identity = json.loads(child_identity_path.read_text(encoding="utf-8-sig"))
            worker_process_id = int(child_identity["pid"])
            worker_started_at_ticks = int(child_identity["started_at_ticks"])
            tool_process_id, tool_started_at_ticks = find_child_process_identity(
                worker_process_id, cwd=case
            )

            stop_process_if_identity_matches(
                worker_process_id, worker_started_at_ticks, cwd=case
            )
            launcher_process.wait(timeout=20)

            self.assertEqual(launcher_process.returncode, 1)
            wait_for_process_exit(
                tool_process_id, cwd=case, started_at_ticks=tool_started_at_ticks
            )
            self.assertFalse(child_completed.exists())
            released_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(released_probe.returncode, 0, released_probe.stdout + released_probe.stderr)
        finally:
            child_release.write_text("release", encoding="ascii")
            if launcher_process.poll() is None:
                launcher_process.terminate()
                launcher_process.wait(timeout=5)
            for process_id, started_at_ticks in (
                (worker_process_id, worker_started_at_ticks),
                (tool_process_id, tool_started_at_ticks),
            ):
                if process_id and started_at_ticks:
                    stop_process_if_identity_matches(process_id, started_at_ticks, cwd=case)
            stdout_handle.close()
            stderr_handle.close()

    @unittest.skipUnless(os.name == "nt", "launcher crash harness requires Windows")
    def test_runner_stops_child_when_launcher_owner_exits(self) -> None:
        case, env = create_runtime_preparation_fixture()
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        windows_dir = case / "packaging" / "windows"
        env["FAKE_UV_BLOCK_STAGE"] = "python"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        child_completed = case / "fake-uv-child-completed.txt"
        child_identity_path = case / "data" / "logs" / "runtime-python.child.json"
        lock_path = case / "data" / "runtime" / "runtime-preparation.lock"
        lock_probe = windows_dir / "probe-runtime-lock-after-launcher-crash.ps1"
        lock_probe.write_text(
            r'''param([string]$LockPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
$stream = Enter-RecallPreparationLock -Path $LockPath
if ($null -eq $stream) { exit 1 }
Exit-RecallPreparationLock -LockStream $stream
exit 0
''',
            encoding="utf-8-sig",
        )
        stdout_handle = (case / "launcher-crash.out.log").open("wb")
        stderr_handle = (case / "launcher-crash.err.log").open("wb")
        launcher_process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Silent",
                "-NoOpen",
            ],
            cwd=case,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
        child_process_id = 0
        child_started_at_ticks = 0
        runner_process_id = 0
        runner_started_at_ticks = 0
        try:
            wait_for_test_path(child_started, launcher_process)
            wait_for_test_path(child_identity_path, launcher_process)
            child_identity = json.loads(child_identity_path.read_text(encoding="utf-8-sig"))
            child_process_id = int(child_identity["pid"])
            child_started_at_ticks = int(child_identity["started_at_ticks"])
            runner_process_id, runner_started_at_ticks = find_runtime_runner_identity(
                launcher_process.pid, cwd=case
            )
            self.assertTrue(
                process_identity_exists(child_process_id, child_started_at_ticks, cwd=case)
            )

            launcher_process.kill()
            launcher_process.wait(timeout=10)
            wait_for_process_exit(
                child_process_id, cwd=case, started_at_ticks=child_started_at_ticks
            )
            wait_for_process_exit(
                runner_process_id, cwd=case, started_at_ticks=runner_started_at_ticks
            )

            self.assertFalse(child_completed.exists())
            released_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(released_probe.returncode, 0, released_probe.stdout + released_probe.stderr)
        finally:
            child_release.write_text("release", encoding="ascii")
            if launcher_process.poll() is None:
                launcher_process.terminate()
                launcher_process.wait(timeout=5)
            for process_id, started_at_ticks in (
                (runner_process_id, runner_started_at_ticks),
                (child_process_id, child_started_at_ticks),
            ):
                if process_id and started_at_ticks:
                    stop_process_if_identity_matches(process_id, started_at_ticks, cwd=case)
            stdout_handle.close()
            stderr_handle.close()

    @unittest.skipUnless(os.name == "nt", "prepare runtime crash harness requires Windows")
    def test_prepare_runtime_owner_crash_kills_tool_before_unlocking(self) -> None:
        case, env = create_runtime_preparation_fixture()
        control = case / "packaging" / "windows" / "control-douyin-recall.ps1"
        windows_dir = case / "packaging" / "windows"
        env["FAKE_UV_BLOCK_STAGE"] = "python"
        child_started = case / "fake-uv-child-started.txt"
        child_release = case / "fake-uv-child-release.txt"
        child_completed = case / "fake-uv-child-completed.txt"
        lock_path = case / "data" / "runtime" / "runtime-preparation.lock"
        lock_probe = windows_dir / "probe-runtime-lock-after-control-crash.ps1"
        lock_probe.write_text(
            r'''param([string]$LockPath)
. "$PSScriptRoot\runtime-preparation-common.ps1"
$stream = Enter-RecallPreparationLock -Path $LockPath
if ($null -eq $stream) { exit 1 }
Exit-RecallPreparationLock -LockStream $stream
exit 0
''',
            encoding="utf-8-sig",
        )
        stdout_handle = (case / "control-crash.out.log").open("wb")
        stderr_handle = (case / "control-crash.err.log").open("wb")
        control_process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(control),
                "-Action",
                "prepare",
                "-NonInteractive",
            ],
            cwd=case,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
        tool_process_id = 0
        tool_started_at_ticks = 0
        try:
            wait_for_test_path(child_started, control_process)
            tool_process_id, tool_started_at_ticks = find_child_process_identity(
                control_process.pid, cwd=case
            )

            control_process.kill()
            control_process.wait(timeout=10)

            wait_for_process_exit(
                tool_process_id, cwd=case, started_at_ticks=tool_started_at_ticks
            )
            self.assertFalse(child_completed.exists())
            released_probe = run_powershell(lock_probe, str(lock_path), env=env)
            self.assertEqual(released_probe.returncode, 0, released_probe.stdout + released_probe.stderr)
        finally:
            child_release.write_text("release", encoding="ascii")
            if control_process.poll() is None:
                control_process.terminate()
                control_process.wait(timeout=5)
            if tool_process_id and tool_started_at_ticks:
                stop_process_if_identity_matches(
                    tool_process_id, tool_started_at_ticks, cwd=case
                )
            stdout_handle.close()
            stderr_handle.close()

    @unittest.skipUnless(os.name == "nt", "startup failure harness requires Windows")
    def test_hidden_first_start_persists_visible_failure_details(self) -> None:
        case, env = create_runtime_preparation_fixture()
        launcher = case / "packaging" / "windows" / "start-douyin-recall.ps1"
        env["FAKE_UV_FAIL_STAGE"] = "python"

        result = run_powershell(
            launcher,
            "-Silent",
            "-NoOpen",
            env=env,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        state = json.loads(
            (case / "data" / "runtime" / "runtime-preparation.json").read_text(
                encoding="utf-8-sig"
            )
        )
        html = (case / "data" / "runtime" / "startup-status.html").read_text(
            encoding="utf-8-sig"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["current_step"], "python")
        self.assertIn("准备失败", html)
        self.assertIn("Python 依赖下载或本地虚拟环境准备失败", html)
        self.assertIn("Douyin Recall Prepare Runtime", html)
        self.assertTrue((case / "data" / "logs" / "runtime-python.err.log").exists())

    def test_control_script_is_ascii_for_windows_powershell_5(self) -> None:
        control = read("control-douyin-recall.ps1")

        try:
            control.encode("ascii")
        except UnicodeEncodeError as exc:
            self.fail(f"control script must stay ASCII for Windows PowerShell 5.1 parsing: {exc}")

    def test_runtime_tool_helpers_are_ascii_for_windows_powershell_5(self) -> None:
        for helper_name in ("runtime-tool-runner.ps1", "runtime-tool-worker.ps1"):
            helper = read(helper_name)
            try:
                helper.encode("ascii")
            except UnicodeEncodeError as exc:
                self.fail(f"{helper_name} must stay ASCII for Windows PowerShell 5.1: {exc}")

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
        self.assertIn("Suggested action", control)
        repair = control[
            control.index("function Repair-StaleServerState"):
            control.index("function Show-ControlMenu")
        ]
        self.assertIn("Invoke-RecallCommand @('repair-state')", repair)
        self.assertNotIn("Remove-Item", repair)
        self.assertIn("Remove-Item -LiteralPath $ProbePath -Force", control)
        self.assertNotIn("Remove-Item -Recurse", control)
        self.assertNotIn("rm -rf", control)
        self.assertIn('"health" { Invoke-HealthCheck; Wait-BeforeExit }', control)
        self.assertIn('"repair" { Repair-StaleServerState; Wait-BeforeExit }', control)

    def test_control_service_audit_reserves_repair_for_stale_pid_records(self) -> None:
        control = read("control-douyin-recall.ps1")
        audit = control[
            control.index("function Get-ServiceAudit"):
            control.index("function Get-ControlSummary")
        ]

        stale_without_listener = audit[
            audit.index('Relation = "stale service record"'):
            audit.index('Relation = "stale service record with listener"')
        ]
        stale_with_listener = audit[
            audit.index('Relation = "stale service record with listener"'):
            audit.index('Relation = "record without listener"')
        ]
        live_without_listener = audit[
            audit.index('Relation = "record without listener"'):
            audit.index('Relation = "own service running"')
        ]
        live_owner_mismatch = audit[
            audit.index('Relation = "recorded PID and port owner mismatch"'):
        ]

        for stale_branch in (stale_without_listener, stale_with_listener):
            self.assertIn('Action = "repair"', stale_branch)
            self.assertIn("Douyin Recall Repair State", stale_branch)

        for live_branch in (live_without_listener, live_owner_mismatch):
            self.assertIn('Action = "stop"', live_branch)
            self.assertIn("Douyin Recall Stop Service", live_branch)
            self.assertNotIn("Douyin Recall Repair State", live_branch)

        health = control[
            control.index("function Invoke-HealthCheck"):
            control.index("function Repair-StaleServerState")
        ]
        self.assertIn('$audit.Action -eq "stop"', health)
        self.assertIn("Run: Douyin Recall Stop Service", health)
        self.assertIn("Run: Douyin Recall Repair State", health)

    def test_control_script_exposes_backup_and_restore_center_actions(self) -> None:
        control = read("control-douyin-recall.ps1")

        self.assertIn("function Create-SqliteBackup", control)
        self.assertIn("function Open-BackupsDirectory", control)
        self.assertIn("function Open-RestoreCenter", control)
        self.assertIn("function Verify-LatestBackup", control)
        self.assertIn("function Test-ManifestRollback", control)
        self.assertIn("function Find-LatestDeliveryManifest", control)
        self.assertIn("function Open-AccountRecovery", control)
        self.assertIn("Create SQLite backup", control)
        self.assertIn("Open backups directory", control)
        self.assertIn("Open restore center", control)
        self.assertIn("Verify latest backup", control)
        self.assertIn("Verify delivery manifest rollback", control)
        self.assertIn("Open account recovery", control)
        self.assertIn("Douyin Recall Backup Now", control)
        self.assertIn("Douyin Recall Backups", control)
        self.assertIn("Douyin Recall Restore Center", control)
        self.assertIn("Douyin Recall Verify Backup", control)
        self.assertIn("Douyin Recall Rollback Check", control)
        self.assertIn("Douyin Recall Account Recovery", control)
        self.assertIn("Backups directory:", control)
        self.assertIn("New-Item -ItemType Directory -Path $ExportsDir -Force", control)
        self.assertIn("Start-Process $ExportsDir", control)
        self.assertIn('"backup" { Create-SqliteBackup; Wait-BeforeExit }', control)
        self.assertIn('"backups" { Open-BackupsDirectory }', control)
        self.assertIn('"restore" { Open-RestoreCenter }', control)
        self.assertIn('"verify-backup" { Verify-LatestBackup; Wait-BeforeExit }', control)
        self.assertIn('"rollback-check" { Test-ManifestRollback; Wait-BeforeExit }', control)
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
        self.assertIn("Last runtime preparation:", control)
        self.assertIn("Preparation stage:", control)
        self.assertIn("Preparation retry: Douyin Recall Prepare Runtime", control)
        self.assertIn("runtime-preparation.json", control)
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
        self.assertIn("Douyin Recall Rollback Check", script)
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
        self.assertIn('-Action ""rollback-check""', script)
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

    def test_release_check_modules_are_outside_wheel_package(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('packages = ["src"]', project)
        for module in RELEASE_TOOL_MODULES:
            self.assertFalse((ROOT / "src" / f"{module}.py").exists(), f"{module} must not be packaged under src")
            self.assertTrue((ROOT / "relcheck" / f"{module}.py").exists(), f"{module} should live under relcheck")

        script_imports = {
            "installed_smoke.py": "from relcheck.installed_smoke import",
            "release_gate.py": "from relcheck.release_gate import",
            "acceptance_matrix.py": "from relcheck.acceptance_matrix import",
            "validate_delivery_evidence.py": "from relcheck.delivery_evidence import",
            "final_release_check.py": "from relcheck.final_release_check import",
            "preflight_summary.py": "from relcheck.preflight_summary import",
            "query_performance_audit.py": "from relcheck.query_performance import",
            "benchmark_web_pages.py": "from relcheck.performance_benchmark import",
            "backup_restore_drill.py": "from relcheck.backup_drill import",
            "database_safety_audit.py": "from relcheck.database_safety import",
        }
        for script_name, expected_import in script_imports.items():
            self.assertIn(expected_import, read_script(script_name))

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

        self.assertIn("D:\\codexDownload\\douyin-release-v0.1.24\\DouyinRecallSetup.exe", script)
        self.assertIn("D:\\codexDownload\\douyin-release-v0.1.24\\installed-qa", script)
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

    def test_upgrade_qa_covers_previous_public_installer_migration_reindex_and_uninstall(self) -> None:
        script = read_script("qa-upgrade-build.ps1")

        self.assertIn("$OldInstallerPath", script)
        self.assertIn("$NewInstallerPath", script)
        self.assertIn('ExpectedVersion "0.1.23"', script)
        self.assertIn('ExpectedVersion "0.1.24"', script)
        self.assertGreaterEqual(script.count('"/NOICONS"'), 1)
        self.assertGreaterEqual(script.count("-AppRoot $appRoot"), 2)
        self.assertIn("D:\\codexDownload\\douyin-release-v0.1.24\\upgrade-qa", script)
        self.assertIn('"seed-legacy-search-db.py"', script)
        self.assertIn('Seed a populated legacy search schema', script)
        self.assertIn('"verify-v0.1.24-upgrade.py"', script)
        self.assertIn('"old_version": "0.1.23"', script)
        self.assertIn('"new_version": "0.1.24"', script)
        self.assertIn('-Label "v0.1.23"', script)
        self.assertIn('-Label "v0.1.24 in-place upgrade"', script)
        self.assertIn("version = \"0.1.24\"", script)
        self.assertIn("$env:TEMP = $tempRoot", script)
        self.assertIn("$env:TMP = $tempRoot", script)
        self.assertIn("$env:HF_HOME = $hfCacheRoot", script)
        self.assertIn("$env:SENTENCE_TRANSFORMERS_HOME", script)
        self.assertIn("CREATE VIRTUAL TABLE favorites_vec USING vec0", script)
        self.assertIn("CREATE VIRTUAL TABLE favorites_fts USING fts5", script)
        self.assertIn("CREATE TABLE uncollect_log", script)
        self.assertIn("FOREIGN KEY (user_id) REFERENCES users(id)", script)
        self.assertIn("FOREIGN KEY (favorite_id) REFERENCES favorites(id)", script)
        self.assertIn("legacy fixture must expose the single-column foreign-key mismatch", script)
        self.assertIn("def foreign_keys(", script)
        self.assertIn('(0, 0, "users", "user_id", "id")', script)
        self.assertIn('(0, 1, parent, item_column, "id")', script)
        self.assertIn('conn.execute("PRAGMA foreign_key_check").fetchall() == []', script)
        self.assertIn('"legacy_log_counts_before_new_writes"', script)
        self.assertIn('"user_id" not in columns(backup, "favorites_fts")', script)
        self.assertIn('"pre-install-recall-*.db"', script)
        self.assertIn("runtime.start_background_workers()", script)
        self.assertIn("pending_before_worker == expected_pending", script)
        self.assertIn("class FakeEncoder", script)
        self.assertIn('"fake_encoder": True', script)
        self.assertIn('hybrid.search_for_kind(', script)
        self.assertIn('successful_jobs == 4', script)
        self.assertIn("Run isolated uninstaller", script)
        self.assertIn('uninstaller removed user database', script)
        self.assertIn("Restore-InnoRegistration -Snapshot $originalRegistration", script)
        self.assertLess(
            script.index("$originalRegistration = Save-InnoRegistration"),
            script.index("-InstallerPath $OldInstallerPath"),
        )
        self.assertNotIn("Remove-Item -Recurse", script)
        self.assertNotIn("rm -rf", script)

    def test_workflow_creates_draft_release_for_exact_binary_qa_on_version_tags(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("tags:", workflow)
        self.assertIn("'v*'", workflow)
        self.assertIn("CHANGELOG.md", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("publish-draft-release:", workflow)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("needs: build-installer", workflow)
        self.assertIn(
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1",
            workflow,
        )
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn('"release", "create"', workflow)
        self.assertIn('"--draft"', workflow)
        self.assertIn("& gh @releaseArgs", workflow)
        self.assertIn("docs/releases/${env:GITHUB_REF_NAME}.md", workflow)
        self.assertIn("--notes-file", workflow)
        self.assertIn("packaging/windows/out/DouyinRecallSetup.exe", workflow)

    def test_installer_workflow_builds_pull_requests_without_publishing_them(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(
            "  pull_request:\n    branches:\n      - main\n  push:\n",
            workflow,
        )
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertNotIn("pull_request_target:", workflow)

    def test_workflows_pin_current_node24_actions(self) -> None:
        release_workflow = WORKFLOW.read_text(encoding="utf-8")
        pr_workflow = PR_CI_WORKFLOW.read_text(encoding="utf-8")
        checkout = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0"

        self.assertIn(checkout, release_workflow)
        self.assertIn(checkout, pr_workflow)
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
            release_workflow,
        )
        self.assertNotIn("actions/checkout@v4", release_workflow)
        self.assertNotIn("actions/upload-artifact@v4", release_workflow)

    def test_pr_ci_is_read_only_and_runs_locked_test_suite(self) -> None:
        workflow = PR_CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn(
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0",
            workflow,
        )
        self.assertIn(
            "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990 # v8.3.2",
            workflow,
        )
        self.assertIn('python-version: "3.11"', workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)
        self.assertIn('version: "0.11.28"', workflow)
        self.assertIn("uv run --locked pytest -q", workflow)

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
        self.assertIn("Douyin Recall Rollback Check", notes)
        self.assertIn("recall verify-backup", notes)
        self.assertIn("rollback-from-manifest", notes)
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
        self.assertIn("SENTENCE_TRANSFORMERS_HOME", doc)
        self.assertIn("HF_HOME", doc)
        self.assertIn(".incomplete", doc)
        self.assertIn("start-douyin-recall.log", doc)
        self.assertIn("uv run python -m src.cli status", doc)
        self.assertIn("uv run python -m src.cli stop", doc)
        self.assertIn("uv run python -m src.cli diagnose", doc)
        self.assertIn("启动前健康检查", doc)
        self.assertIn("安装器里准备 Python 依赖和 Playwright Chromium", doc)
        self.assertIn("5 个真实阶段", doc)
        self.assertIn("已准备好的正常日常启动仍保持隐藏", doc)
        self.assertIn("运行环境尚未准备或 fingerprint 已变化时", doc)
        self.assertIn("服务启动失败时仍会打开失败页", doc)
        self.assertIn("同一页面自动跳转到 `http://127.0.0.1:<端口>`", doc)
        self.assertIn("选择“重试”立即再试", doc)
        self.assertIn("已通过精确 revision", doc)
        self.assertIn("prepare-runtime.log", doc)
        self.assertIn("Douyin Recall Control", doc)
        self.assertIn("Douyin Recall Stop Service", doc)
        self.assertIn("状态摘要", doc)
        self.assertIn("健康检查", doc)
        self.assertIn("Douyin Recall Health Check", doc)
        self.assertIn("Douyin Recall Repair State", doc)
        self.assertIn("record_without_listener", doc)
        self.assertIn("record_port_mismatch", doc)
        self.assertIn("此时不要使用 Repair State", doc)
        self.assertIn("也不要使用 Repair State", doc)
        self.assertIn("Douyin Recall Prepare Runtime", doc)
        self.assertIn("不会启动本地 Web 服务", doc)
        self.assertIn("Douyin Recall Backup Now", doc)
        self.assertIn("Douyin Recall Backups", doc)
        self.assertIn("Douyin Recall Restore Center", doc)
        self.assertIn("Douyin Recall Verify Backup", doc)
        self.assertIn("Douyin Recall Rollback Check", doc)
        self.assertIn("uv run python -m src.cli verify-backup", doc)
        self.assertIn("uv run python -m src.cli rollback-from-manifest", doc)
        self.assertIn("Douyin Recall Account Recovery", doc)
        self.assertIn("/auth", doc)
        self.assertIn("pre-install-recall-", doc)
        self.assertIn("data\\exports", doc)
        self.assertIn("/maintenance", doc)


if __name__ == "__main__":
    unittest.main()

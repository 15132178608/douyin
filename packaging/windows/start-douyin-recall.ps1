param(
    [string]$OpenPath = "/",
    [switch]$Silent,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # Console encoding setup is best effort; startup should continue.
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:UV_NO_DEV = "1"
$env:NO_COLOR = "1"

$LauncherPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $LauncherPath
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$RuntimeDir = Join-Path $DataRoot "runtime"
$LogsDir = Join-Path $DataRoot "logs"
$StartLog = Join-Path $LogsDir "start-douyin-recall.log"
$StartupStatusPath = Join-Path $RuntimeDir "startup-status.html"
$StartupWaitStatusPath = Join-Path $RuntimeDir "startup-status-waiting.html"
$StartupFailureStatusPath = Join-Path $RuntimeDir "startup-status-launcher-failed.html"
$PreparationStatePath = Join-Path $RuntimeDir "runtime-preparation.json"
$PreparationLockPath = Join-Path $RuntimeDir "runtime-preparation.lock"
$RuntimePreparedPath = Join-Path $RuntimeDir "runtime-prepared.json"
$EnvPath = Join-Path $AppRoot ".env"
$EnvExamplePath = Join-Path $AppRoot ".env.example"
$PyProjectPath = Join-Path $AppRoot "pyproject.toml"
$UvLockPath = Join-Path $AppRoot "uv.lock"
$VenvPython = Join-Path $AppRoot ".venv\Scripts\python.exe"
$PlaywrightBrowsersJsonPath = Join-Path $AppRoot ".venv\Lib\site-packages\playwright\driver\package\browsers.json"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvDownloadDir = Join-Path $DownloadRoot "uv"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"
$HuggingFaceCacheDir = Join-Path $DownloadRoot "huggingface"
$SentenceTransformersCacheDir = Join-Path $HuggingFaceCacheDir "sentence-transformers"
$UvInstallScriptUrl = "https://astral.sh/uv/install.ps1"
$RuntimeCommonScript = Join-Path $ScriptDir "runtime-preparation-common.ps1"
$RuntimeToolRunnerScript = Join-Path $ScriptDir "runtime-tool-runner.ps1"
$RuntimeToolWorkerScript = Join-Path $ScriptDir "runtime-tool-worker.ps1"
foreach ($requiredRuntimeScript in @($RuntimeCommonScript, $RuntimeToolRunnerScript, $RuntimeToolWorkerScript)) {
    if (-not (Test-Path -LiteralPath $requiredRuntimeScript -PathType Leaf)) {
        throw "Missing runtime preparation helper: $requiredRuntimeScript"
    }
}
. $RuntimeCommonScript
$script:CurrentStartupStep = ""
$script:CurrentStartupStepKey = ""
$script:StartupStepTotal = 7
$script:StartupStepIndex = 0
$script:StartupStartedAt = Get-Date
$script:StartupStatusOpened = $false
$script:PreparationLockStream = $null
$script:OwnsPreparationLock = $false
$script:StartupStatusSteps = @(
    [pscustomobject]@{ Key = "environment"; Label = "检查本地环境"; Detail = "准备本地运行目录，确认安装目录、日志目录和运行时缓存可写。"; Status = "waiting" },
    [pscustomobject]@{ Key = "config"; Label = "检查本地配置"; Detail = "首次运行会从 .env.example 创建本地配置。"; Status = "waiting" },
    [pscustomobject]@{ Key = "uv"; Label = "定位 uv 运行时"; Detail = "如果本机没有 uv，会为当前 Windows 用户安装。"; Status = "waiting" },
    [pscustomobject]@{ Key = "python"; Label = "准备 Python 运行环境"; Detail = "准备 Python 依赖，执行 uv sync，首次运行可能需要下载依赖。"; Status = "waiting" },
    [pscustomobject]@{ Key = "browser"; Label = "下载/安装 Playwright Chromium"; Detail = "准备 Playwright Chromium，浏览器运行时会缓存到 D:\codexDownload\douyinclaude-runtime。"; Status = "waiting" },
    [pscustomobject]@{ Key = "database"; Label = "初始化本地数据库"; Detail = "准备本地 SQLite 数据库。"; Status = "waiting" },
    [pscustomobject]@{ Key = "service"; Label = "启动本地 Web 服务"; Detail = "确认服务状态，只在需要时启动本地 Web。"; Status = "waiting" }
)

function Write-StartLog {
    param([string]$Message)
    try {
        if (Test-Path $LogsDir) {
            $timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
            Add-Content -Path $StartLog -Value "[$timestamp] $Message" -Encoding UTF8
        }
    }
    catch {
        # Startup logging must never prevent the app from launching.
    }
}

function Write-Step {
    param([string]$Message)
    $script:CurrentStartupStep = $Message
    Write-Host ""
    Write-Host "==> $Message"
    Write-StartLog $Message
}

function ConvertTo-HtmlText {
    param([string]$Value)

    if ($null -eq $Value) {
        return ""
    }
    return [System.Net.WebUtility]::HtmlEncode($Value)
}

function Set-StartupStatusStep {
    param(
        [string]$Key,
        [string]$Status,
        [string]$Detail = ""
    )

    foreach ($step in $script:StartupStatusSteps) {
        if ($step.Key -eq $Key) {
            $step.Status = $Status
            if (-not [string]::IsNullOrWhiteSpace($Detail)) {
                $step.Detail = $Detail
            }
        }
    }
}

function Write-StartupStatusPage {
    param(
        [string]$Summary,
        [string]$Detail = "",
        [string]$Tone = "running",
        [switch]$Final,
        [string]$RedirectUrl = "",
        [string]$Path = $StartupStatusPath
    )

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $refresh = if (-not [string]::IsNullOrWhiteSpace($RedirectUrl)) {
        $safeRedirectUrl = ConvertTo-HtmlText $RedirectUrl
        "<meta http-equiv='refresh' content='1;url=$safeRedirectUrl'>"
    }
    elseif ($Final) { "" } else { '<meta http-equiv="refresh" content="2">' }
    $doneCount = @($script:StartupStatusSteps | Where-Object { $_.Status -eq "done" }).Count
    $overallPercent = [int][Math]::Floor(($doneCount * 100) / [Math]::Max(1, $script:StartupStepTotal))
    if ($Tone -eq "done") {
        $overallPercent = 100
    }
    $stepHtml = foreach ($step in $script:StartupStatusSteps) {
        $status = ConvertTo-HtmlText $step.Status
        $label = ConvertTo-HtmlText $step.Label
        $stepDetail = ConvertTo-HtmlText $step.Detail
        $activity = if ($step.Status -eq "running") { "<div class='activity'><span></span></div>" } else { "" }
        "<li class='step $status'><span class='dot'></span><div><strong>$label</strong><small>$status</small><p>$stepDetail</p>$activity</div></li>"
    }
    $summaryText = ConvertTo-HtmlText $Summary
    $detailText = ConvertTo-HtmlText $Detail
    $cacheText = ConvertTo-HtmlText $DownloadRoot
    $logText = ConvertTo-HtmlText $StartLog
    $logsText = ConvertTo-HtmlText $LogsDir
    $html = @"
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  $refresh
  <title>正在准备 Douyin Recall</title>
  <style>
    :root { color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #1d2433; }
    main { max-width: 860px; margin: 40px auto; padding: 0 24px; }
    h1 { margin: 0 0 8px; font-size: 30px; }
    .summary { padding: 18px 20px; border-radius: 8px; background: #fff; border: 1px solid #d9e0ea; }
    .summary.running { border-left: 6px solid #2563eb; }
    .summary.done { border-left: 6px solid #16803c; }
    .summary.failed { border-left: 6px solid #c2410c; }
    .summary p { margin: 8px 0 0; color: #556070; line-height: 1.5; }
    .overall { margin-top: 14px; height: 8px; overflow: hidden; border-radius: 999px; background: #e4e9f1; }
    .overall span { display: block; width: $overallPercent%; height: 100%; background: linear-gradient(90deg, #2563eb, #06b6d4); transition: width .2s ease; }
    .steps { list-style: none; margin: 20px 0; padding: 0; display: grid; gap: 10px; }
    .step { display: grid; grid-template-columns: 18px 1fr; gap: 12px; padding: 14px 16px; background: #fff; border: 1px solid #d9e0ea; border-radius: 8px; }
    .dot { width: 12px; height: 12px; border-radius: 50%; background: #a8b1bf; margin-top: 4px; }
    .step.running .dot { background: #2563eb; }
    .step.done .dot { background: #16803c; }
    .step.failed .dot { background: #c2410c; }
    .step strong { display: block; font-size: 16px; }
    .step small { display: inline-block; margin-top: 4px; color: #667085; }
    .step p { margin: 8px 0 0; color: #556070; line-height: 1.45; }
    .activity { height: 5px; margin-top: 10px; overflow: hidden; border-radius: 999px; background: #e4e9f1; }
    .activity span { display: block; width: 35%; height: 100%; background: #2563eb; animation: activity 1.2s ease-in-out infinite alternate; }
    @keyframes activity { from { transform: translateX(-20%); } to { transform: translateX(220%); } }
    .meta { margin-top: 20px; padding: 16px; background: #eef2f7; border-radius: 8px; color: #3a4658; line-height: 1.7; }
    code { font-family: Consolas, monospace; }
  </style>
</head>
<body>
  <main>
    <h1>正在准备 Douyin Recall</h1>
    <section class="summary $Tone">
      <strong>$summaryText</strong>
      <p>$detailText</p>
      <div class="overall"><span></span></div>
      <p>已完成阶段：$doneCount / $script:StartupStepTotal</p>
    </section>
    <ol class="steps">
      $($stepHtml -join "`n      ")
    </ol>
    <section class="meta">
      <div>运行时缓存：<code>$cacheText</code></div>
      <div>启动日志：<code>$logText</code></div>
      <div>服务日志：<code>$logsText</code></div>
      <div>诊断命令：<code>uv run python -m src.cli diagnose</code></div>
      <div>重试入口：<code>Douyin Recall Prepare Runtime</code></div>
    </section>
  </main>
</body>
</html>
"@
    Write-RecallTextAtomic -Path $Path -Value $html
}

function Show-StartupStatusPage {
    param([string]$Path = $StartupStatusPath)

    if ($script:StartupStatusOpened) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if ($NoOpen) {
        $script:StartupStatusOpened = $true
        Write-StartLog "First-run progress page opening suppressed by -NoOpen: $Path"
        return
    }
    try {
        Start-Process $Path
        $script:StartupStatusOpened = $true
        Write-StartLog "Opened first-run progress page: $Path"
    }
    catch {
        Write-StartLog "Could not open first-run progress page $Path`: $($_.Exception.Message)"
    }
}

function Write-PreparationStateBestEffort {
    param(
        [string]$Status,
        [string]$Summary,
        [string]$Detail = "",
        [string]$ErrorSummary = "",
        [string]$RecommendedAction = ""
    )

    try {
        $completed = @($script:StartupStatusSteps | Where-Object { $_.Status -eq "done" } | ForEach-Object { $_.Key })
        $stepLabel = $script:CurrentStartupStep
        Write-RecallPreparationState `
            -Path $PreparationStatePath `
            -Status $Status `
            -StepKey $script:CurrentStartupStepKey `
            -StepLabel $stepLabel `
            -StepIndex $script:StartupStepIndex `
            -StepTotal $script:StartupStepTotal `
            -Summary $Summary `
            -Detail $Detail `
            -ErrorSummary $ErrorSummary `
            -RecommendedAction $RecommendedAction `
            -StartedAt $script:StartupStartedAt `
            -CompletedSteps $completed
    }
    catch {
        Write-StartLog "Could not persist runtime preparation state: $($_.Exception.Message)"
    }
}

function Update-StartupStatus {
    param(
        [string]$Key,
        [string]$Status,
        [string]$Summary,
        [string]$Detail = "",
        [string]$Tone = "running",
        [switch]$Final,
        [string]$RedirectUrl = ""
    )

    if ($Key) {
        $script:CurrentStartupStepKey = $Key
        Set-StartupStatusStep -Key $Key -Status $Status -Detail $Detail
    }
    $stateStatus = if ($Tone -eq "failed") { "failed" } elseif ($Tone -eq "done") { "ready" } else { "running" }
    Write-PreparationStateBestEffort -Status $stateStatus -Summary $Summary -Detail $Detail
    Write-StartupStatusPage -Summary $Summary -Detail $Detail -Tone $Tone -Final:$Final -RedirectUrl $RedirectUrl
}

function Write-StartupProgress {
    param(
        [string]$Message,
        [string]$Key = "",
        [switch]$LongRunning
    )

    $script:StartupStepIndex += 1
    $script:CurrentStartupStep = $Message
    if ($Key) {
        $script:CurrentStartupStepKey = $Key
        Set-StartupStatusStep -Key $Key -Status "running"
    }
    Write-Host ""
    Write-Host "进度：[$script:StartupStepIndex/$script:StartupStepTotal] $Message"
    if ($LongRunning) {
        Write-Host "提示：首次运行可能需要几分钟，取决于网络和缓存状态。"
    }
    Write-StartLog "Progress [$script:StartupStepIndex/$script:StartupStepTotal] $Message"
    if ($Key) {
        $detail = if ($LongRunning) { "首次运行可能需要几分钟，取决于网络和缓存状态。" } else { "" }
        Update-StartupStatus -Key $Key -Status "running" -Summary $Message -Detail $detail
    }
}

function Get-LatestToolOutput {
    param([string[]]$Paths)

    $lines = @()
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) {
            continue
        }
        try {
            $lines += Get-Content -LiteralPath $path -Tail 12 -Encoding UTF8 -ErrorAction Stop
        }
        catch {
            # The child process may be flushing this file; the next heartbeat will retry.
        }
    }
    $line = $lines |
        ForEach-Object { ([string]$_ -replace '\x1B\[[0-?]*[ -/]*[@-~]', '').Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($line)) {
        return ""
    }
    $line = $line -replace '\s+', ' '
    if ($line.Length -gt 280) {
        $line = $line.Substring(0, 280) + "..."
    }
    return $line
}

function Invoke-StartupTool {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$Key,
        [string]$Summary
    )

    $stdoutPath = Join-Path $LogsDir "runtime-$Key.out.log"
    $stderrPath = Join-Path $LogsDir "runtime-$Key.err.log"
    $exitCodePath = Join-Path $LogsDir "runtime-$Key.exit-code.txt"
    $toolExitCodePath = Join-Path $LogsDir "runtime-$Key.tool-exit-code.txt"
    $childIdentityPath = Join-Path $LogsDir "runtime-$Key.child.json"
    $runnerGatePath = Join-Path $LogsDir "runtime-$Key.runner-go"
    $workerSpecPath = Join-Path $LogsDir "runtime-$Key.worker.json"
    $runnerSpecPath = Join-Path $LogsDir "runtime-$Key.runner.json"
    foreach ($path in @(
        $stdoutPath,
        $stderrPath,
        $exitCodePath,
        $toolExitCodePath,
        $childIdentityPath,
        $runnerGatePath,
        $workerSpecPath,
        $runnerSpecPath
    )) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $runnerPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $toolSpec = [ordered]@{
        file_path = $FilePath
        arguments = @($ArgumentList)
        working_directory = $AppRoot
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        tool_exit_code_path = $toolExitCodePath
    }
    Write-RecallJsonAtomic -Path $workerSpecPath -Value $toolSpec
    $runnerSpec = [ordered]@{
        powershell_path = $runnerPowerShell
        worker_script_path = $RuntimeToolWorkerScript
        worker_spec_path = $workerSpecPath
        stderr_path = $stderrPath
        exit_code_path = $exitCodePath
        tool_exit_code_path = $toolExitCodePath
        child_identity_path = $childIdentityPath
        start_gate_path = $runnerGatePath
        owner_pid = $PID
        owner_started_at_ticks = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    Write-RecallJsonAtomic -Path $runnerSpecPath -Value $runnerSpec
    $runnerArguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$RuntimeToolRunnerScript`"",
        "-SpecPath",
        "`"$runnerSpecPath`""
    )
    $startedAt = Get-Date
    $process = $null
    $exitCode = $null
    $runnerProtocolCompleted = $false
    $runnerAssignedToJob = $false
    $jobHandle = [IntPtr]::Zero
    try {
        $jobHandle = New-RecallKillOnCloseJob
        $process = Start-Process `
            -FilePath $runnerPowerShell `
            -ArgumentList $runnerArguments `
            -WorkingDirectory $AppRoot `
            -WindowStyle Hidden `
            -PassThru
        Add-RecallProcessToJob -JobHandle $jobHandle -ProcessHandle $process.Handle
        $runnerAssignedToJob = $true
        [IO.File]::WriteAllText($runnerGatePath, "go", (New-Object Text.UTF8Encoding($false)))
        do {
            $elapsed = [int][Math]::Max(0, ((Get-Date) - $startedAt).TotalSeconds)
            $latest = Get-LatestToolOutput -Paths @($stdoutPath, $stderrPath)
            $detail = "已运行 $elapsed 秒；正在等待工具完成。"
            if (-not [string]::IsNullOrWhiteSpace($latest)) {
                $detail = "已运行 $elapsed 秒；最新输出：$latest"
            }
            try {
                Update-StartupStatus -Key $Key -Status "running" -Summary $Summary -Detail $detail
            }
            catch {
                Write-StartLog "Could not refresh startup progress while $Summary was running: $($_.Exception.Message)"
            }
            if (-not $process.HasExited) {
                Start-Sleep -Milliseconds 900
            }
        } while (-not $process.HasExited)
        $process.WaitForExit()
        if (-not (Test-Path -LiteralPath $exitCodePath -PathType Leaf)) {
            throw "$Summary runner exited without writing $exitCodePath"
        }
        $exitCodeText = (Get-Content -LiteralPath $exitCodePath -Raw -Encoding UTF8).Trim()
        $parsedExitCode = 0
        if (-not [int]::TryParse($exitCodeText, [ref]$parsedExitCode)) {
            throw "$Summary runner wrote an invalid exit code: $exitCodeText"
        }
        $exitCode = $parsedExitCode
        $runnerProtocolCompleted = $true
    }
    finally {
        if (-not $runnerProtocolCompleted -and $jobHandle -ne [IntPtr]::Zero) {
            Close-RecallRuntimeJob -JobHandle $jobHandle
            $jobHandle = [IntPtr]::Zero
        }
        if ($null -ne $process) {
            try {
                if (-not $runnerProtocolCompleted) {
                    Write-StartLog "Cleaning runtime process tree because $Summary monitoring did not complete."
                    if ($runnerAssignedToJob) {
                        if (-not $process.HasExited) {
                            $process.WaitForExit(5000) | Out-Null
                        }
                    }
                    elseif (-not $process.HasExited) {
                        $process.Kill()
                    }
                    if (-not $process.HasExited -and -not $process.WaitForExit(5000)) {
                        $process.Kill()
                    }
                }
            }
            catch {
                Write-StartLog "Could not finish child process cleanup for $Summary`: $($_.Exception.Message)"
            }
            $process.Dispose()
        }
        if ($jobHandle -ne [IntPtr]::Zero) {
            Close-RecallRuntimeJob -JobHandle $jobHandle
            $jobHandle = [IntPtr]::Zero
        }
    }
    $latest = Get-LatestToolOutput -Paths @($stdoutPath, $stderrPath)
    Write-StartLog "$Summary finished with exit code $exitCode. Output: $latest"
    if ($exitCode -ne 0) {
        throw "$Summary failed with exit code $exitCode. See $stdoutPath and $stderrPath"
    }
    return $latest
}

function Test-DirectoryWritable {
    param(
        [string]$Name,
        [string]$Path,
        [string]$FixHint
    )

    Write-Step "启动前健康检查：$Name"
    try {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        $ProbePath = Join-Path $Path ".douyin-recall-write-test.tmp"
        Set-Content -Path $ProbePath -Value "ok" -Encoding UTF8
        Remove-Item -LiteralPath $ProbePath -Force
    }
    catch {
        throw "$Name 失败：无法写入 $Path。$FixHint。原始错误：$($_.Exception.Message)"
    }
}

function Test-UvAvailable {
    if ($env:UV_EXE -and (Test-Path $env:UV_EXE)) {
        return $true
    }
    if (Get-Command "uv.exe" -ErrorAction SilentlyContinue) {
        return $true
    }
    $userUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    return (Test-Path $userUv)
}

function Test-WebEndpoint {
    param(
        [string]$Name,
        [string]$Uri,
        [string]$FixHint
    )

    Write-Step "启动前健康检查：$Name"
    try {
        Invoke-WebRequest -Uri $Uri -UseBasicParsing -Method Head -TimeoutSec 10 | Out-Null
    }
    catch {
        try {
            Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 10 | Out-Null
        }
        catch {
            throw "$Name 失败：无法访问 $Uri。$FixHint。原始错误：$($_.Exception.Message)"
        }
    }
}

function Test-WebReady {
    param(
        [string]$Url,
        [int]$TimeoutSec = 2
    )

    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-PortOwnerProcess {
    param([int]$Port)

    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
    }
    catch {
        Write-StartLog "Could not inspect port $Port owner: $($_.Exception.Message)"
        return $null
    }
    if ($null -eq $connection -or -not $connection.OwningProcess) {
        return $null
    }
    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
    }
    catch {
        Write-StartLog "Could not inspect process $($connection.OwningProcess): $($_.Exception.Message)"
        return $null
    }
}

function Test-DouyinRecallServiceProcess {
    param([object]$ProcessInfo)

    if ($null -eq $ProcessInfo) {
        return $false
    }
    $commandLine = [string]$ProcessInfo.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }
    return ($commandLine -like "*src.cli*" -and $commandLine -like "*serve*")
}

function Test-RecordedCurrentService {
    param(
        [object]$ProcessInfo,
        [int]$Port
    )

    if ($null -eq $ProcessInfo) {
        return $false
    }
    $serverStatePath = Join-Path $RuntimeDir "server.json"
    if (-not (Test-Path $serverStatePath)) {
        return $false
    }
    try {
        $state = Get-Content -Path $serverStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $statePid = [int]$state.pid
        $statePort = [int]$state.port
        if ($statePid -ne [int]$ProcessInfo.ProcessId -or $statePort -ne $Port) {
            return $false
        }
        if ($state.PSObject.Properties["started_at"] -and (Test-Path $LauncherPath)) {
            $startedAt = ([datetime]::Parse([string]$state.started_at)).ToUniversalTime()
            $launcherUpdatedAt = (Get-Item -LiteralPath $LauncherPath).LastWriteTimeUtc
            if ($startedAt -lt $launcherUpdatedAt.AddSeconds(-2)) {
                Write-StartLog "Recorded Douyin Recall service predates this launcher; treating pid=$($ProcessInfo.ProcessId) as stale."
                return $false
            }
        }
        return $true
    }
    catch {
        Write-StartLog "Could not read current service state: $($_.Exception.Message)"
        return $false
    }
}

function Test-RecordedServiceStateCurrent {
    param([int]$Port)

    $serverStatePath = Join-Path $RuntimeDir "server.json"
    if (-not (Test-Path $serverStatePath)) {
        return $false
    }
    try {
        $state = Get-Content -Path $serverStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$state.port -ne $Port) {
            return $false
        }
        if ($state.PSObject.Properties["pid"]) {
            try {
                Get-Process -Id ([int]$state.pid) -ErrorAction Stop | Out-Null
            }
            catch {
                return $false
            }
        }
        if ($state.PSObject.Properties["started_at"] -and (Test-Path $LauncherPath)) {
            $startedAt = ([datetime]::Parse([string]$state.started_at)).ToUniversalTime()
            $launcherUpdatedAt = (Get-Item -LiteralPath $LauncherPath).LastWriteTimeUtc
            if ($startedAt -lt $launcherUpdatedAt.AddSeconds(-2)) {
                Write-StartLog "Recorded Douyin Recall service predates this launcher; inspecting port owner before reuse."
                return $false
            }
        }
        return $true
    }
    catch {
        Write-StartLog "Could not read lightweight service state: $($_.Exception.Message)"
        return $false
    }
}

function Stop-StaleDouyinRecallServiceOnPort {
    param([int]$Port)

    $owner = Get-PortOwnerProcess -Port $Port
    if ($null -eq $owner) {
        return $false
    }
    if (Test-RecordedCurrentService -ProcessInfo $owner -Port $Port) {
        Write-StartLog "Port $Port is owned by the recorded current Douyin Recall service pid=$($owner.ProcessId)."
        return $false
    }
    if (-not (Test-DouyinRecallServiceProcess -ProcessInfo $owner)) {
        Write-StartLog "Port $Port is owned by a non-Douyin Recall process pid=$($owner.ProcessId)."
        return $false
    }
    Write-Step "Stopping stale Douyin Recall service on port $Port (pid=$($owner.ProcessId))"
    try {
        Stop-Process -Id $owner.ProcessId -Force
        Start-Sleep -Milliseconds 800
        return $true
    }
    catch {
        Write-StartLog "Could not stop stale Douyin Recall service pid=$($owner.ProcessId): $($_.Exception.Message)"
        return $false
    }
}

function Wait-WebReady {
    param(
        [string]$Url,
        [object]$Process = $null,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
    while ((Get-Date) -lt $deadline) {
        if (Test-WebReady -Url $Url -TimeoutSec 2) {
            return $true
        }

        if ($null -ne $Process) {
            try {
                if ($Process.HasExited) {
                    throw "Douyin Recall Web service exited early with code $($Process.ExitCode). See $LogsDir\serve.err.log"
                }
            }
            catch {
                if ($_.Exception.Message -like "Douyin Recall Web service exited early*") {
                    throw
                }
            }
        }

        Start-Sleep -Milliseconds 500
    }

    throw "Timeout waiting for Douyin Recall Web service at $Url. See $LogsDir\serve.err.log"
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-RecallFileSha256 -Path $Path)
}

function Get-RuntimeFingerprint {
    return (Get-RecallRuntimeFingerprint -PyProjectPath $PyProjectPath -UvLockPath $UvLockPath)
}

function Test-PlaywrightChromiumReady {
    return (Test-RecallPlaywrightChromiumReady `
        -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
        -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath)
}

function Test-RuntimePrepared {
    return (Test-RecallRuntimePrepared `
        -RuntimePreparedPath $RuntimePreparedPath `
        -VenvPython $VenvPython `
        -EnvPath $EnvPath `
        -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
        -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath `
        -PyProjectPath $PyProjectPath `
        -UvLockPath $UvLockPath)
}

function Write-RuntimePreparedMarker {
    Write-RecallRuntimePreparedMarker `
        -RuntimePreparedPath $RuntimePreparedPath `
        -PyProjectPath $PyProjectPath `
        -UvLockPath $UvLockPath
}

function Invoke-PreparedRecallCli {
    param([string[]]$RecallArgs)

    & $VenvPython "-m" "src.cli" @RecallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$VenvPython -m src.cli $($RecallArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Start-RecallServiceProcess {
    param([switch]$UsePreparedRuntime)

    $stdout = Join-Path $LogsDir "serve.out.log"
    $stderr = Join-Path $LogsDir "serve.err.log"
    if ($UsePreparedRuntime -and (Test-Path $VenvPython)) {
        Write-Step "Starting local web server with prepared Python: $VenvPython -m src.cli serve"
        return Start-Process -FilePath $VenvPython -ArgumentList @("-m", "src.cli", "serve") -WorkingDirectory $AppRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    }

    $serveStartInfo = Get-RecallCliStartInfo @("serve")
    Write-Step "Starting local web server with: $($serveStartInfo.CommandText)"
    return Start-Process -FilePath $uv -ArgumentList $serveStartInfo.ArgumentList -WorkingDirectory $AppRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
}

function Open-DouyinRecall {
    param(
        [string]$BaseUrl,
        [string]$PathToOpen
    )

    if (-not $PathToOpen.StartsWith("/")) {
        $PathToOpen = "/$PathToOpen"
    }
    $openUrl = "$BaseUrl$PathToOpen"

    Write-Step "Opening $openUrl"
    Write-Host ""
    Write-Host "维护中心：$BaseUrl/maintenance"
    Write-Host "停止服务：uv run python -m src.cli stop"
    Write-Host "排障日志：$StartLog"
    if ($NoOpen) {
        Write-StartLog "Browser opening suppressed by -NoOpen: $openUrl"
        return
    }
    if ($script:StartupStatusOpened) {
        Write-StartLog "First-run progress page will redirect to $openUrl"
        return
    }
    Start-Process $openUrl
}

function Wait-SetupQrReady {
    param(
        [string]$BaseUrl,
        [string]$OpenPath,
        [int]$TimeoutSeconds = 8
    )

    if ($OpenPath -ne "/" -and $OpenPath -ne "/setup") {
        return $false
    }

    $setupUrl = "$BaseUrl/setup"
    $statusUrl = "$BaseUrl/setup/auth-status"
    Write-Step "Prewarming first-run QR before opening the browser"
    try {
        Invoke-WebRequest -Uri $setupUrl -UseBasicParsing -TimeoutSec 3 | Out-Null
    }
    catch {
        Write-StartLog "First-run QR prewarm skipped: $($_.Exception.Message)"
        return $false
    }

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $statusUrl -UseBasicParsing -TimeoutSec 3
            $content = [string]$response.Content
            if ($content -like "*/auth/qr-image*" -or $content -like "*data-auth-success*") {
                Write-StartLog "First-run QR is ready before opening browser."
                return $true
            }
            if ($content -like "*二维码生成失败*" -or $content -like "*dot-failed*") {
                Write-StartLog "First-run QR prewarm reached a failure state before opening browser."
                return $false
            }
        }
        catch {
            Write-StartLog "First-run QR status poll failed: $($_.Exception.Message)"
            return $false
        }
        Start-Sleep -Milliseconds 250
    }

    Write-StartLog "First-run QR was not ready before browser open timeout."
    return $false
}

function Assert-StartupPreflight {
    Test-DirectoryWritable `
        -Name "安装目录可写" `
        -Path $AppRoot `
        -FixHint "请检查当前 Windows 用户是否有安装目录写入权限，或重新安装到当前用户目录"
    Test-DirectoryWritable `
        -Name "日志目录可写" `
        -Path $LogsDir `
        -FixHint "请检查安装目录下 data\logs 的写入权限"
    Test-DirectoryWritable `
        -Name "运行时缓存目录可写" `
        -Path $DownloadRoot `
        -FixHint "请检查 D:\codexDownload 的写入权限，或手动创建该目录后重试"

    if (-not (Test-UvAvailable)) {
        Test-WebEndpoint `
            -Name "uv 下载入口可访问" `
            -Uri $UvInstallScriptUrl `
            -FixHint "请检查网络、代理或防火墙；如果公司网络拦截，请先配置代理后重试"
    }
    else {
        Write-Step "启动前健康检查：uv 下载入口可访问"
        Write-Host "uv 已安装，跳过下载入口检查。"
    }
}

function Find-Uv {
    if ($env:UV_EXE -and (Test-Path $env:UV_EXE)) {
        return (Resolve-Path $env:UV_EXE).Path
    }

    $command = Get-Command "uv.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $userUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $userUv) {
        return (Resolve-Path $userUv).Path
    }

    Write-Step "uv not found; installing uv for the current Windows user"
    New-Item -ItemType Directory -Path $UvDownloadDir -Force | Out-Null
    $installer = Join-Path $UvDownloadDir "install-uv.ps1"
    Invoke-WebRequest -Uri $UvInstallScriptUrl -OutFile $installer
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer

    if (Test-Path $userUv) {
        return (Resolve-Path $userUv).Path
    }

    $command = Get-Command "uv.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "uv installation finished, but uv.exe was not found. Restart Windows or install uv manually."
}

function Get-RecallCliCommandText {
    param([string[]]$RecallArgs)

    return "uv run python -m src.cli $($RecallArgs -join ' ')"
}

function Invoke-RecallCli {
    param([string[]]$RecallArgs)

    & $uv "run" "python" "-m" "src.cli" @RecallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$(Get-RecallCliCommandText -RecallArgs $RecallArgs) failed with exit code $LASTEXITCODE"
    }
}

function Get-RecallCliStartInfo {
    param([string[]]$RecallArgs)

    return [pscustomobject]@{
        CommandText = Get-RecallCliCommandText -RecallArgs $RecallArgs
        ArgumentList = @("run", "python", "-m", "src.cli") + $RecallArgs
    }
}

function Get-WebPort {
    if (-not (Test-Path $EnvPath)) {
        return 8000
    }

    $match = Select-String -Path $EnvPath -Pattern "^\s*WEB_PORT\s*=\s*(\d+)\s*$" | Select-Object -First 1
    if ($match -and $match.Matches.Count -gt 0) {
        return [int]$match.Matches[0].Groups[1].Value
    }
    return 8000
}

function Write-Troubleshooting {
    param([int]$Port = 8000)

    Write-Host ""
    Write-Host "常用恢复命令："
    Write-Host "  uv run python -m src.cli status"
    Write-Host "  uv run python -m src.cli stop"
    Write-Host "  uv run python -m src.cli diagnose"
    Write-Host ""
    Write-Host "维护中心： http://127.0.0.1:$port/maintenance"
    Write-Host "启动日志： $StartLog"
    Write-Host "服务日志： $LogsDir"
    Write-Host "运行时下载/缓存： $DownloadRoot"
}

function Get-StartupFailureInfo {
    param(
        [string]$ErrorMessage,
        [int]$Port = 8000
    )

    $step = $script:CurrentStartupStep
    if ([string]::IsNullOrWhiteSpace($step)) {
        $step = "尚未记录启动阶段"
    }

    $combined = "$step $ErrorMessage"
    $cause = "启动流程在当前阶段失败，需要结合日志确认原始错误。"
    $next = "先运行开始菜单里的 Douyin Recall Prepare Runtime；如果仍失败，再运行 uv run python -m src.cli diagnose 导出诊断。"

    if ($combined -like "*uv sync*") {
        $cause = "Python 依赖下载或本地虚拟环境准备失败，常见原因是网络、代理、缓存或安装目录写入权限。"
        $next = "确认网络和 D:\codexDownload\douyinclaude-runtime 可写后，运行 Douyin Recall Prepare Runtime 重试。"
    }
    elseif ($combined -like "*playwright install chromium*" -or $combined -like "*playwright*chromium*") {
        $cause = "Playwright Chromium 浏览器运行时下载或安装失败，常见原因是网络、代理或运行时缓存目录不可写。"
        $next = "确认网络可访问 Playwright 下载源后，运行 Douyin Recall Prepare Runtime 重试。"
    }
    elseif ($combined -like "*uv not found*" -or $combined -like "*uv.exe*" -or $combined -like "*install-uv*") {
        $cause = "uv 安装或发现失败，常见原因是网络、代理、PATH 未刷新或当前用户安装目录不可写。"
        $next = "检查网络/代理后重新打开 Douyin Recall Prepare Runtime，必要时重启 Windows 让 PATH 生效。"
    }
    elseif ($combined -like "*python -m src.cli init-db*" -or $combined -like "*recall init-db*") {
        $cause = "本地数据库初始化失败，常见原因是安装目录或 data 目录无写入权限。"
        $next = "确认安装目录可写后运行 Douyin Recall Prepare Runtime；仍失败时运行 uv run python -m src.cli diagnose。"
    }
    elseif ($combined -like "*python -m src.cli serve*" -or $combined -like "*recall serve*") {
        $cause = "本地 Web 服务启动失败，常见原因是端口占用、旧状态文件残留或服务日志里有应用错误。"
        $next = "先运行 Douyin Recall Health Check 或 Douyin Recall Repair State，再查看 $LogsDir 中的 serve.err.log。"
    }

    $errorSummary = ($ErrorMessage -replace '\s+', ' ').Trim()
    if ($errorSummary.Length -gt 500) {
        $errorSummary = $errorSummary.Substring(0, 500) + "..."
    }
    return [pscustomobject]@{
        Step = $step
        Cause = $cause
        Next = $next
        ErrorSummary = $errorSummary
        Port = $Port
    }
}

function Write-StartupFailureHint {
    param([object]$FailureInfo)

    Write-Host "失败阶段：$($FailureInfo.Step)" -ForegroundColor Yellow
    Write-Host "可能原因：$($FailureInfo.Cause)"
    Write-Host "建议下一步：$($FailureInfo.Next)"
    Write-Host "错误摘要：$($FailureInfo.ErrorSummary)"
    Write-Host "维护中心：http://127.0.0.1:$($FailureInfo.Port)/maintenance"
    Write-Host "诊断命令：uv run python -m src.cli diagnose"
    Write-Host "运行时下载/缓存：$DownloadRoot"
    Write-Host "启动日志：$StartLog"
    Write-Host "服务日志：$LogsDir"
}

try {
    Set-Location $AppRoot
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightBrowsersDir -Force | Out-Null
    New-Item -ItemType Directory -Path $HuggingFaceCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $SentenceTransformersCacheDir -Force | Out-Null
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:UV_LINK_MODE = "copy"
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
    $env:HF_HOME = $HuggingFaceCacheDir
    $env:SENTENCE_TRANSFORMERS_HOME = $SentenceTransformersCacheDir
    Write-StartLog "Startup requested from $AppRoot"

    $script:PreparationLockStream = Enter-RecallPreparationLock -Path $PreparationLockPath
    if ($null -eq $script:PreparationLockStream) {
        Write-StartLog "Another runtime preparation already owns $PreparationLockPath; opening a fresh follower page."
        $ownerDetail = "另一个安装器或 Douyin Recall Prepare Runtime 正在准备运行环境。为避免并发修改，本次启动不会重复执行；准备完成后请重新打开 Douyin Recall。状态文件：$PreparationStatePath"
        try {
            if (Test-Path -LiteralPath $PreparationStatePath) {
                $ownerState = Get-Content -LiteralPath $PreparationStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
                if (-not [string]::IsNullOrWhiteSpace([string]$ownerState.summary)) {
                    $ownerDetail = "$ownerDetail 当前状态：$([string]$ownerState.summary)"
                }
            }
        }
        catch {
            Write-StartLog "Could not read the active preparation state: $($_.Exception.Message)"
        }
        $script:CurrentStartupStep = "等待另一个运行环境准备任务"
        $script:CurrentStartupStepKey = "environment"
        Set-StartupStatusStep -Key "environment" -Status "running" -Detail $ownerDetail
        Write-StartupStatusPage -Summary "已有运行环境准备任务正在进行" -Detail $ownerDetail -Tone "running" -Final -Path $StartupWaitStatusPath
        Show-StartupStatusPage -Path $StartupWaitStatusPath
        exit 0
    }
    $script:OwnsPreparationLock = $true

    $createdEnv = $false
    if (-not (Test-Path $EnvPath)) {
        if (-not (Test-Path $EnvExamplePath)) {
            throw "Missing .env.example in $AppRoot"
        }
        Write-Step "Creating .env from .env.example"
        Copy-Item -Path $EnvExamplePath -Destination $EnvPath
        $createdEnv = $true
    }

    $port = Get-WebPort
    $url = "http://127.0.0.1:$port"
    if (Test-WebReady -Url $url -TimeoutSec 1) {
        if (Test-RecordedServiceStateCurrent -Port $port) {
            Write-Step "Douyin Recall is already running; opening browser without runtime preparation"
            Open-DouyinRecall -BaseUrl $url -PathToOpen $OpenPath
            exit 0
        }
        Write-StartLog "Local endpoint is reachable but service state is missing or stale; inspecting port owner before reuse."
    }
    Stop-StaleDouyinRecallServiceOnPort -Port $port | Out-Null
    if (Test-WebReady -Url $url -TimeoutSec 1) {
        Write-Step "Douyin Recall is already running; opening browser without runtime preparation"
        Open-DouyinRecall -BaseUrl $url -PathToOpen $OpenPath
        exit 0
    }

    if (Test-RuntimePrepared) {
        Write-Step "运行环境已准备，跳过 uv sync 和 Playwright 安装"
        if (-not (Test-Path (Join-Path $DataRoot "recall.db"))) {
            Write-StartupProgress -Message "初始化本地数据库：prepared python -m src.cli init-db" -Key "database"
            Invoke-PreparedRecallCli @("init-db")
            Update-StartupStatus -Key "database" -Status "done" -Summary "本地数据库已就绪"
        }
        Write-StartupProgress -Message "启动本地 Web 服务：prepared python -m src.cli serve" -Key "service"
        $serverProcess = Start-RecallServiceProcess -UsePreparedRuntime
        Wait-WebReady -Url $url -Process $serverProcess -TimeoutSeconds 60 | Out-Null
        Write-RuntimePreparedMarker
        Update-StartupStatus -Key "service" -Status "done" -Summary "准备完成" -Detail "Douyin Recall 本地 Web 界面即将打开。" -Tone "done" -Final
        Open-DouyinRecall -BaseUrl $url -PathToOpen $OpenPath
        exit 0
    }

    if (Test-Path -LiteralPath $RuntimePreparedPath) {
        Remove-Item -LiteralPath $RuntimePreparedPath -Force
        Write-StartLog "Invalidated the previous runtime-prepared marker before full preparation."
    }

    Write-StartupProgress -Message "检查本地配置文件" -Key "config"
    $configDetail = if ($createdEnv) { "已从 .env.example 创建本地配置。" } else { "本地配置文件已经存在。" }
    Update-StartupStatus -Key "config" -Status "done" -Summary "本地配置检查完成" -Detail $configDetail

    Write-StartupProgress -Message "检查本地环境" -Key "environment"
    Update-StartupStatus -Key "environment" -Status "running" -Summary "检查本地环境" -Detail "正在确认安装目录、日志目录和运行时缓存目录。"
    Show-StartupStatusPage
    Assert-StartupPreflight
    Update-StartupStatus -Key "environment" -Status "done" -Summary "本地环境检查完成" -Detail "安装目录、日志目录和运行时缓存目录可用。"

    Write-Host ""
    Write-Host "提示：当前安装包未签名，Windows SmartScreen 可能提示风险；请只使用 GitHub Release 页面下载的安装包。"
    Write-Host "提示：首次启动会下载 Python 依赖和 Playwright 浏览器，缓存目录：$DownloadRoot"
    Write-Host "提示：首次生成搜索索引时还会下载本地模型，耗时取决于网络。"

    Write-StartupProgress -Message "定位 uv 运行时" -Key "uv"
    $uv = Find-Uv
    Update-StartupStatus -Key "uv" -Status "done" -Summary "uv 运行时已就绪"

    Write-StartupProgress -Message "准备 Python 运行环境：uv sync" -Key "python" -LongRunning
    $pythonOutput = Invoke-StartupTool -FilePath $uv -ArgumentList @("sync", "--no-dev", "--color", "never") -Key "python" -Summary "准备 Python 运行环境：uv sync"
    $pythonDetail = if ([string]::IsNullOrWhiteSpace($pythonOutput)) { "Python 依赖已经同步。" } else { "完成：$pythonOutput" }
    Update-StartupStatus -Key "python" -Status "done" -Summary "Python 运行环境已就绪" -Detail $pythonDetail

    Write-StartupProgress -Message "下载/安装 Playwright Chromium：uv run playwright install chromium" -Key "browser" -LongRunning
    if (Test-PlaywrightChromiumReady) {
        $browserOutput = "当前 Playwright manifest 所需的浏览器组件已经完整就绪。"
    }
    else {
        $browserOutput = Invoke-StartupTool -FilePath $uv -ArgumentList @("run", "playwright", "install", "chromium") -Key "browser" -Summary "下载/安装 Playwright Chromium"
        if (-not (Test-PlaywrightChromiumReady)) {
            Write-StartLog "Browser post-install validation was incomplete; retrying with playwright install --force chromium."
            $browserOutput = Invoke-StartupTool -FilePath $uv -ArgumentList @("run", "playwright", "install", "--force", "chromium") -Key "browser" -Summary "强制修复 Playwright Chromium"
        }
        if (-not (Test-PlaywrightChromiumReady)) {
            throw "playwright install chromium completed, but the required browser components failed exact post-install validation"
        }
    }
    $browserDetail = if ([string]::IsNullOrWhiteSpace($browserOutput)) { "Playwright Chromium 已安装。" } else { "完成：$browserOutput" }
    Update-StartupStatus -Key "browser" -Status "done" -Summary "Playwright Chromium 已就绪" -Detail $browserDetail

    Write-StartupProgress -Message "初始化本地数据库：uv run python -m src.cli init-db" -Key "database"
    Invoke-RecallCli @("init-db")
    Update-StartupStatus -Key "database" -Status "done" -Summary "本地数据库已就绪"
    Write-RuntimePreparedMarker

    Write-StartupProgress -Message "启动本地 Web 服务：uv run python -m src.cli serve" -Key "service"
    if (Test-WebReady -Url $url -TimeoutSec 2) {
        Write-Step "Douyin Recall is already running"
    }
    else {
        $serverProcess = Start-RecallServiceProcess
        Wait-WebReady -Url $url -Process $serverProcess -TimeoutSeconds 60 | Out-Null
    }
    $openPathNormalized = if ($OpenPath.StartsWith("/")) { $OpenPath } else { "/$OpenPath" }
    $redirectUrl = "$url$openPathNormalized"
    Update-StartupStatus -Key "service" -Status "done" -Summary "准备完成" -Detail "Douyin Recall 本地 Web 界面即将打开。" -Tone "done" -Final -RedirectUrl $redirectUrl
    Open-DouyinRecall -BaseUrl $url -PathToOpen $OpenPath
}
catch {
    Write-StartLog "Startup failed: $($_.Exception.Message)"
    $port = 8000
    try {
        $port = Get-WebPort
    }
    catch {
        Write-StartLog "Could not resolve WEB_PORT during failure handling: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "Douyin Recall failed to start:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    $failedKey = $script:CurrentStartupStepKey
    if ([string]::IsNullOrWhiteSpace($failedKey)) {
        $failedKey = "environment"
    }
    $failureInfo = Get-StartupFailureInfo -ErrorMessage $_.Exception.Message -Port $port
    $failureDetail = "失败阶段：$($failureInfo.Step)。可能原因：$($failureInfo.Cause) 建议下一步：$($failureInfo.Next) 错误摘要：$($failureInfo.ErrorSummary)"
    $failurePagePath = $StartupFailureStatusPath
    if ($script:OwnsPreparationLock) {
        $failurePagePath = $StartupStatusPath
        try {
            Update-StartupStatus -Key $failedKey -Status "failed" -Summary "准备失败" -Detail $failureDetail -Tone "failed" -Final
        }
        catch {
            Write-StartLog "Could not persist the startup failure page: $($_.Exception.Message)"
        }
        Write-PreparationStateBestEffort -Status "failed" -Summary "准备失败" -Detail $failureDetail -ErrorSummary $failureInfo.ErrorSummary -RecommendedAction $failureInfo.Next
    }
    else {
        try {
            Set-StartupStatusStep -Key $failedKey -Status "failed" -Detail $failureDetail
            Write-StartupStatusPage -Summary "启动入口失败" -Detail $failureDetail -Tone "failed" -Final -Path $StartupFailureStatusPath
        }
        catch {
            Write-StartLog "Could not persist the independent launcher failure page: $($_.Exception.Message)"
        }
    }
    Show-StartupStatusPage -Path $failurePagePath
    Write-StartupFailureHint -FailureInfo $failureInfo
    Write-Host ""
    Write-Troubleshooting -Port $port
    if (-not $Silent) {
        Read-Host "Press Enter to close"
    }
    exit 1
}
finally {
    Exit-RecallPreparationLock -LockStream $script:PreparationLockStream
    $script:PreparationLockStream = $null
    $script:OwnsPreparationLock = $false
}

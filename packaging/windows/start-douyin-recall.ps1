param(
    [string]$OpenPath = "/"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$RuntimeDir = Join-Path $DataRoot "runtime"
$LogsDir = Join-Path $DataRoot "logs"
$StartLog = Join-Path $LogsDir "start-douyin-recall.log"
$StartupStatusPath = Join-Path $RuntimeDir "startup-status.html"
$EnvPath = Join-Path $AppRoot ".env"
$EnvExamplePath = Join-Path $AppRoot ".env.example"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvDownloadDir = Join-Path $DownloadRoot "uv"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"
$UvInstallScriptUrl = "https://astral.sh/uv/install.ps1"
$script:CurrentStartupStep = ""
$script:CurrentStartupStepKey = ""
$script:StartupStepTotal = 7
$script:StartupStepIndex = 0
$script:StartupStatusOpened = $false
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
        [string]$Status
    )

    foreach ($step in $script:StartupStatusSteps) {
        if ($step.Key -eq $Key) {
            $step.Status = $Status
        }
    }
}

function Write-StartupStatusPage {
    param(
        [string]$Summary,
        [string]$Detail = "",
        [string]$Tone = "running",
        [switch]$Final
    )

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $refresh = if ($Final) { "" } else { '<meta http-equiv="refresh" content="2">' }
    $stepHtml = foreach ($step in $script:StartupStatusSteps) {
        $status = ConvertTo-HtmlText $step.Status
        $label = ConvertTo-HtmlText $step.Label
        $stepDetail = ConvertTo-HtmlText $step.Detail
        "<li class='step $status'><span class='dot'></span><div><strong>$label</strong><small>$status</small><p>$stepDetail</p></div></li>"
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
    .steps { list-style: none; margin: 20px 0; padding: 0; display: grid; gap: 10px; }
    .step { display: grid; grid-template-columns: 18px 1fr; gap: 12px; padding: 14px 16px; background: #fff; border: 1px solid #d9e0ea; border-radius: 8px; }
    .dot { width: 12px; height: 12px; border-radius: 50%; background: #a8b1bf; margin-top: 4px; }
    .step.running .dot { background: #2563eb; }
    .step.done .dot { background: #16803c; }
    .step.failed .dot { background: #c2410c; }
    .step strong { display: block; font-size: 16px; }
    .step small { display: inline-block; margin-top: 4px; color: #667085; }
    .step p { margin: 8px 0 0; color: #556070; line-height: 1.45; }
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
    </section>
    <ol class="steps">
      $($stepHtml -join "`n      ")
    </ol>
    <section class="meta">
      <div>运行时缓存：<code>$cacheText</code></div>
      <div>启动日志：<code>$logText</code></div>
      <div>服务日志：<code>$logsText</code></div>
      <div>诊断命令：<code>uv run recall diagnose</code></div>
      <div>重试入口：<code>Douyin Recall Prepare Runtime</code></div>
    </section>
  </main>
</body>
</html>
"@
    Set-Content -Path $StartupStatusPath -Value $html -Encoding UTF8
}

function Show-StartupStatusPage {
    if (-not $script:StartupStatusOpened -and (Test-Path $StartupStatusPath)) {
        Start-Process $StartupStatusPath
        $script:StartupStatusOpened = $true
    }
}

function Update-StartupStatus {
    param(
        [string]$Key,
        [string]$Status,
        [string]$Summary,
        [string]$Detail = "",
        [string]$Tone = "running",
        [switch]$OpenPage,
        [switch]$Final
    )

    if ($Key) {
        $script:CurrentStartupStepKey = $Key
        Set-StartupStatusStep -Key $Key -Status $Status
    }
    Write-StartupStatusPage -Summary $Summary -Detail $Detail -Tone $Tone -Final:$Final
    if ($OpenPage) {
        Show-StartupStatusPage
    }
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
    Write-Host "  uv run recall status"
    Write-Host "  uv run recall stop"
    Write-Host "  uv run recall diagnose"
    Write-Host ""
    Write-Host "维护中心： http://127.0.0.1:$port/maintenance"
    Write-Host "启动日志： $StartLog"
    Write-Host "服务日志： $LogsDir"
    Write-Host "运行时下载/缓存： $DownloadRoot"
}

function Write-StartupFailureHint {
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
    $next = "先运行开始菜单里的 Douyin Recall Prepare Runtime；如果仍失败，再运行 uv run recall diagnose 导出诊断。"

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
    elseif ($combined -like "*recall init-db*") {
        $cause = "本地数据库初始化失败，常见原因是安装目录或 data 目录无写入权限。"
        $next = "确认安装目录可写后运行 Douyin Recall Prepare Runtime；仍失败时运行 uv run recall diagnose。"
    }
    elseif ($combined -like "*recall serve*") {
        $cause = "本地 Web 服务启动失败，常见原因是端口占用、旧状态文件残留或服务日志里有应用错误。"
        $next = "先运行 Douyin Recall Health Check 或 Douyin Recall Repair State，再查看 $LogsDir 中的 serve.err.log。"
    }

    Write-Host "失败阶段：$step" -ForegroundColor Yellow
    Write-Host "可能原因：$cause"
    Write-Host "建议下一步：$next"
    Write-Host "维护中心：http://127.0.0.1:$Port/maintenance"
    Write-Host "诊断命令：uv run recall diagnose"
    Write-Host "运行时下载/缓存：$DownloadRoot"
    Write-Host "启动日志：$StartLog"
    Write-Host "服务日志：$LogsDir"
}

try {
    Set-Location $AppRoot
    Write-StartupProgress -Message "检查本地环境" -Key "environment"
    Update-StartupStatus -Key "environment" -Status "running" -Summary "检查本地环境" -Detail "正在确认安装目录、日志目录和运行时缓存目录。" -OpenPage
    Assert-StartupPreflight
    Update-StartupStatus -Key "environment" -Status "done" -Summary "本地环境检查完成" -Detail "安装目录、日志目录和运行时缓存目录可用。"
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightBrowsersDir -Force | Out-Null
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:UV_LINK_MODE = "copy"
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
    Write-StartLog "Startup requested from $AppRoot"

    Write-Host ""
    Write-Host "提示：当前安装包未签名，Windows SmartScreen 可能提示风险；请只使用 GitHub Release 页面下载的安装包。"
    Write-Host "提示：首次启动会下载 Python 依赖和 Playwright 浏览器，缓存目录：$DownloadRoot"
    Write-Host "提示：首次生成搜索索引时还会下载本地模型，耗时取决于网络。"

    Write-StartupProgress -Message "检查本地配置文件" -Key "config"
    if (-not (Test-Path $EnvPath)) {
        if (-not (Test-Path $EnvExamplePath)) {
            throw "Missing .env.example in $AppRoot"
        }
        Write-Step "Creating .env from .env.example"
        Copy-Item -Path $EnvExamplePath -Destination $EnvPath
    }
    Update-StartupStatus -Key "config" -Status "done" -Summary "本地配置检查完成"

    Write-StartupProgress -Message "定位 uv 运行时" -Key "uv"
    $uv = Find-Uv
    Update-StartupStatus -Key "uv" -Status "done" -Summary "uv 运行时已就绪"

    Write-StartupProgress -Message "准备 Python 运行环境：uv sync" -Key "python" -LongRunning
    & $uv "sync"
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE"
    }
    Update-StartupStatus -Key "python" -Status "done" -Summary "Python 运行环境已就绪"

    Write-StartupProgress -Message "下载/安装 Playwright Chromium：uv run playwright install chromium" -Key "browser" -LongRunning
    & $uv "run" "playwright" "install" "chromium"
    if ($LASTEXITCODE -ne 0) {
        throw "playwright install chromium failed with exit code $LASTEXITCODE"
    }
    Update-StartupStatus -Key "browser" -Status "done" -Summary "Playwright Chromium 已就绪"

    Write-StartupProgress -Message "初始化本地数据库：uv run recall init-db" -Key "database"
    & $uv "run" "recall" "init-db"
    if ($LASTEXITCODE -ne 0) {
        throw "recall init-db failed with exit code $LASTEXITCODE"
    }
    Update-StartupStatus -Key "database" -Status "done" -Summary "本地数据库已就绪"

    $port = Get-WebPort
    $url = "http://127.0.0.1:$port"

    Write-StartupProgress -Message "启动本地 Web 服务：uv run recall status" -Key "service"
    & $uv "run" "recall" "status"

    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        Write-Step "Douyin Recall is already running"
    }
    catch {
        $stdout = Join-Path $LogsDir "serve.out.log"
        $stderr = Join-Path $LogsDir "serve.err.log"
        Write-Step "Starting local web server with: uv run recall serve"
        Start-Process -FilePath $uv -ArgumentList @("run", "recall", "serve") -WorkingDirectory $AppRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Start-Sleep -Seconds 3
    }
    Update-StartupStatus -Key "service" -Status "done" -Summary "准备完成" -Detail "Douyin Recall 本地 Web 界面即将打开。" -Tone "done" -Final

    if (-not $OpenPath.StartsWith("/")) {
        $OpenPath = "/$OpenPath"
    }
    $openUrl = "$url$OpenPath"

    Write-Step "Opening $openUrl"
    Write-Host ""
    Write-Host "维护中心：$url/maintenance"
    Write-Host "停止服务：uv run recall stop"
    Write-Host "排障日志：$StartLog"
    Start-Process $openUrl
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
    Update-StartupStatus -Key $failedKey -Status "failed" -Summary "准备失败" -Detail "失败阶段：$script:CurrentStartupStep。建议运行 Douyin Recall Prepare Runtime，或执行 uv run recall diagnose 导出诊断。" -Tone "failed" -Final
    Write-StartupFailureHint -ErrorMessage $_.Exception.Message -Port $port
    Write-Host ""
    Write-Troubleshooting -Port $port
    Read-Host "Press Enter to close"
    exit 1
}

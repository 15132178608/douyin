param(
    [ValidateSet("menu", "start", "stop", "status", "maintenance", "diagnose", "logs", "update")]
    [string]$Action = "menu"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$LogsDir = Join-Path $DataRoot "logs"
$EnvPath = Join-Path $AppRoot ".env"
$ProjectPath = Join-Path $AppRoot "pyproject.toml"
$ServerStatePath = Join-Path $AppRoot "data\runtime\server.json"
$StartScript = Join-Path $ScriptDir "start-douyin-recall.ps1"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"

function Write-Header {
    param([string]$Title)

    Write-Host ""
    Write-Host "==> $Title"
}

function Initialize-RuntimeEnvironment {
    Set-Location $AppRoot
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightBrowsersDir -Force | Out-Null
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:UV_LINK_MODE = "copy"
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
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

    throw "没有找到 uv.exe。请先点击开始菜单里的 Douyin Recall 完成首次启动，或手动安装 uv 后重试。"
}

function Invoke-RecallCommand {
    param([string[]]$RecallArgs)

    Initialize-RuntimeEnvironment
    $uv = Find-Uv
    & $uv "run" "recall" @RecallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "uv run recall $($RecallArgs -join ' ') failed with exit code $LASTEXITCODE"
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

function Test-WebAvailable {
    param([string]$Url)

    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-InstalledVersion {
    if (-not (Test-Path $ProjectPath)) {
        return "unknown"
    }

    $match = Select-String -Path $ProjectPath -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match -and $match.Matches.Count -gt 0) {
        return $match.Matches[0].Groups[1].Value
    }
    return "unknown"
}

function Read-ServerState {
    if (-not (Test-Path $ServerStatePath)) {
        return $null
    }

    try {
        return Get-Content -Path $ServerStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Test-RecordedProcessRunning {
    param([object]$State)

    if ($null -eq $State) {
        return $false
    }

    try {
        $PidProperty = $State.PSObject.Properties["pid"]
        if ($null -eq $PidProperty -or $null -eq $PidProperty.Value) {
            return $false
        }

        $ProcessId = [int]$PidProperty.Value
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-ControlSummary {
    $port = Get-WebPort
    $homeUrl = "http://127.0.0.1:$port"
    $maintenanceUrl = "$homeUrl/maintenance"
    $state = Read-ServerState
    $recordedPid = $null
    $webAvailable = Test-WebAvailable -Url $homeUrl
    $serviceStatus = "未运行"

    if ($null -ne $state) {
        $PidProperty = $state.PSObject.Properties["pid"]
        if ($null -ne $PidProperty) {
            $recordedPid = $PidProperty.Value
        }

        if (Test-RecordedProcessRunning -State $state) {
            if ($webAvailable) {
                $serviceStatus = "正在运行"
            }
            else {
                $serviceStatus = "进程存在，但本地网页暂未响应"
            }
        }
        else {
            $serviceStatus = "PID 记录陈旧"
        }
    }
    elseif ($webAvailable) {
        $serviceStatus = "网页可访问，但没有 PID 记录"
    }

    return [pscustomobject]@{
        Version = Get-InstalledVersion
        ServiceStatus = $serviceStatus
        Port = $port
        MaintenanceUrl = $maintenanceUrl
        LogsDir = $LogsDir
        DownloadRoot = $DownloadRoot
        RecordedPid = $recordedPid
    }
}

function Write-ControlSummary {
    $summary = Get-ControlSummary

    Write-Header "本地状态摘要"
    Write-Host "当前版本：$($summary.Version)"
    Write-Host "服务状态：$($summary.ServiceStatus)"
    if ($null -ne $summary.RecordedPid) {
        Write-Host "记录 PID：$($summary.RecordedPid)"
    }
    Write-Host "维护中心：$($summary.MaintenanceUrl)"
    Write-Host "日志目录：$($summary.LogsDir)"
    Write-Host "运行时缓存：$($summary.DownloadRoot)"

    if ($summary.ServiceStatus -eq "正在运行" -or $summary.ServiceStatus -eq "进程存在，但本地网页暂未响应") {
        Write-Host "停止入口：Douyin Recall Stop Service"
    }
    else {
        Write-Host "启动入口：Douyin Recall"
    }
}

function Wait-BeforeExit {
    Read-Host "Press Enter to close" | Out-Null
}

function Start-DouyinRecall {
    if (-not (Test-Path $StartScript)) {
        throw "Missing launcher script: $StartScript"
    }

    Write-Header "启动 Douyin Recall"
    & $StartScript
}

function Open-MaintenanceCenter {
    $port = Get-WebPort
    $url = "http://127.0.0.1:$port/maintenance"

    Write-Header "打开维护中心"
    if (Test-WebAvailable -Url $url) {
        Start-Process $url
        return
    }

    Write-Host "本地 Web 服务还没有响应，先启动服务再打开维护中心。"
    & $StartScript -OpenPath "/maintenance"
}

function Open-LogsDirectory {
    Initialize-RuntimeEnvironment
    Write-Header "打开日志目录"
    Write-Host $LogsDir
    Start-Process $LogsDir
}

function Show-Status {
    Write-ControlSummary
    Write-Header "服务状态"
    Invoke-RecallCommand @('status')
    $port = Get-WebPort
    Write-Host ""
    Write-Host "维护中心：http://127.0.0.1:$port/maintenance"
    Write-Host "日志目录：$LogsDir"
}

function Stop-DouyinRecall {
    Write-Header "停止本地 Web 服务"
    Invoke-RecallCommand @('stop')
}

function Export-Diagnostics {
    Write-Header "导出诊断包"
    Invoke-RecallCommand @('diagnose')
}

function Check-Update {
    Write-Header "检查更新"
    Invoke-RecallCommand @('update')
}

function Show-ControlMenu {
    Write-ControlSummary

    while ($true) {
        Write-Host ""
        Write-Host "Douyin Recall Control"
        Write-Host "1. 启动并打开 Web"
        Write-Host "2. 打开维护中心"
        Write-Host "3. 查看服务状态"
        Write-Host "4. 停止本地 Web 服务"
        Write-Host "5. 导出诊断包"
        Write-Host "6. 打开日志目录"
        Write-Host "7. 检查更新"
        Write-Host "0. 退出"
        $choice = Read-Host "请选择"

        switch ($choice) {
            "1" { Start-DouyinRecall; return }
            "2" { Open-MaintenanceCenter; return }
            "3" { Show-Status; Wait-BeforeExit; return }
            "4" { Stop-DouyinRecall; Wait-BeforeExit; return }
            "5" { Export-Diagnostics; Wait-BeforeExit; return }
            "6" { Open-LogsDirectory; return }
            "7" { Check-Update; Wait-BeforeExit; return }
            "0" { return }
            default { Write-Host "无效选择，请重新输入。" -ForegroundColor Yellow }
        }
    }
}

try {
    switch ($Action) {
        "menu" { Show-ControlMenu }
        "start" { Start-DouyinRecall }
        "maintenance" { Open-MaintenanceCenter }
        "status" { Show-Status; Wait-BeforeExit }
        "stop" { Stop-DouyinRecall; Wait-BeforeExit }
        "diagnose" { Export-Diagnostics; Wait-BeforeExit }
        "logs" { Open-LogsDirectory }
        "update" { Check-Update; Wait-BeforeExit }
    }
}
catch {
    Write-Host ""
    Write-Host "Douyin Recall Control failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "安装目录：$AppRoot"
    Write-Host "日志目录：$LogsDir"
    Write-Host "运行时缓存：$DownloadRoot"
    Wait-BeforeExit
    exit 1
}

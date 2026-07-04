$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$LogsDir = Join-Path $DataRoot "logs"
$EnvPath = Join-Path $AppRoot ".env"
$EnvExamplePath = Join-Path $AppRoot ".env.example"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvDownloadDir = Join-Path $DownloadRoot "uv"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
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
    Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -OutFile $installer
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

try {
    Set-Location $AppRoot
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightBrowsersDir -Force | Out-Null
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir

    Write-Host ""
    Write-Host "提示：当前安装包未签名，Windows SmartScreen 可能提示风险；请只使用 GitHub Release 页面下载的安装包。"
    Write-Host "提示：首次启动会下载 Python 依赖和 Playwright 浏览器，缓存目录：$DownloadRoot"
    Write-Host "提示：首次生成搜索索引时还会下载本地模型，耗时取决于网络。"

    if (-not (Test-Path $EnvPath)) {
        if (-not (Test-Path $EnvExamplePath)) {
            throw "Missing .env.example in $AppRoot"
        }
        Write-Step "Creating .env from .env.example"
        Copy-Item -Path $EnvExamplePath -Destination $EnvPath
    }

    $uv = Find-Uv

    Write-Step "Installing Python dependencies with: uv sync"
    & $uv "sync"
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE"
    }

    Write-Step "Installing browser runtime with: uv run playwright install chromium"
    & $uv "run" "playwright" "install" "chromium"
    if ($LASTEXITCODE -ne 0) {
        throw "playwright install chromium failed with exit code $LASTEXITCODE"
    }

    Write-Step "Initializing local database"
    & $uv "run" "recall" "init-db"
    if ($LASTEXITCODE -ne 0) {
        throw "recall init-db failed with exit code $LASTEXITCODE"
    }

    $port = Get-WebPort
    $url = "http://127.0.0.1:$port"

    Write-Step "Checking local web server status with: uv run recall status"
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

    Write-Step "Opening $url"
    Start-Process $url
}
catch {
    Write-Host ""
    Write-Host "Douyin Recall failed to start:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Logs are in: $LogsDir"
    Read-Host "Press Enter to close"
    exit 1
}

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$LogsDir = Join-Path $DataRoot "logs"
$EnvPath = Join-Path $AppRoot ".env"
$EnvExamplePath = Join-Path $AppRoot ".env.example"

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
    $installer = Join-Path $env:TEMP "install-uv.ps1"
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

param(
    [string]$AppRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $AppRoot = (Get-Location).Path
}

$SourceDb = Join-Path $AppRoot "data\recall.db"
$BackupDir = Join-Path $AppRoot "data\exports"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"

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

    return $null
}

if (-not (Test-Path $SourceDb)) {
    Write-Host "Pre-install backup skipped: recall.db not found."
    exit 0
}

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
$env:UV_CACHE_DIR = $UvCacheDir
$env:UV_LINK_MODE = "copy"

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupPath = Join-Path $BackupDir "pre-install-recall-$Timestamp.db"

$python = @'
import sqlite3
import sys
from pathlib import Path

source_path = sys.argv[1]
backup_path = sys.argv[2]
Path(backup_path).parent.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect(source_path)
destination = sqlite3.connect(backup_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
'@

$uv = Find-Uv
if ($uv) {
    try {
        & $uv "run" "python" "-c" $python $SourceDb $BackupPath
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Pre-install database backup: $BackupPath"
            exit 0
        }
        Write-Host "SQLite backup helper exited with code $LASTEXITCODE; falling back to file copy."
    }
    catch {
        Write-Host "SQLite backup helper failed: $($_.Exception.Message)"
    }
}
else {
    Write-Host "uv.exe was not found; falling back to file copy."
}

Copy-Item -LiteralPath $SourceDb -Destination $BackupPath -Force
Write-Host "Pre-install database backup fallback copy: $BackupPath"

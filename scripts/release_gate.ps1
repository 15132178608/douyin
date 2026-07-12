param(
    [string]$OutputDir = "",
    [switch]$BuildInstaller,
    [string]$InstallerPath = "",
    [switch]$ContinueOnFailure,
    [switch]$UpdatePerformanceBaseline,
    [int]$KeepReleaseEvidence = 8,
    [switch]$SkipEvidenceCleanup
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DownloadRoot = "D:\codexDownload\douyinclaude-release-gate"
if (-not (Test-Path -LiteralPath $DownloadRoot)) {
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:UV_CACHE_DIR) {
    $env:UV_CACHE_DIR = Join-Path $DownloadRoot "uv-cache"
}
if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $DownloadRoot "ms-playwright"
}
if (-not $env:UV_LINK_MODE) {
    $env:UV_LINK_MODE = "copy"
}

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

$script = Join-Path $ProjectRoot "scripts\release_gate.py"
$argsList = @($script)
if ($OutputDir) {
    $argsList += @("--output-dir", $OutputDir)
}
if ($BuildInstaller) {
    $argsList += "--build-installer"
}
if ($InstallerPath) {
    $argsList += @("--installer-path", $InstallerPath)
}
if ($ContinueOnFailure) {
    $argsList += "--continue-on-failure"
}
if ($UpdatePerformanceBaseline) {
    $argsList += "--update-performance-baseline"
}
$argsList += @("--keep-release-evidence", "$KeepReleaseEvidence")
if ($SkipEvidenceCleanup) {
    $argsList += "--skip-evidence-cleanup"
}

Push-Location $ProjectRoot
try {
    & $python @argsList
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

param(
    [string]$SourceRoot,
    [string]$InnoSetupCompiler
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) {
    $SourceRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$InstallerScript = Join-Path $ScriptDir "DouyinRecall.iss"
$OutputDir = Join-Path $ScriptDir "out"
$SetupExe = Join-Path $OutputDir "DouyinRecallSetup.exe"

function Find-InnoSetupCompiler {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        if (Test-Path $ExplicitPath) {
            return (Resolve-Path $ExplicitPath).Path
        }
        throw "ISCC.exe was not found at: $ExplicitPath"
    }

    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "ISCC.exe was not found. Install Inno Setup 6.5 or later, then rerun packaging\windows\build-installer.ps1."
}

if (-not (Test-Path (Join-Path $SourceRoot "pyproject.toml"))) {
    throw "SourceRoot does not look like the project root: $SourceRoot"
}
if (-not (Test-Path $InstallerScript)) {
    throw "Missing installer script: $InstallerScript"
}

$ProjectText = Get-Content -Raw -LiteralPath (Join-Path $SourceRoot "pyproject.toml")
$LockText = Get-Content -Raw -LiteralPath (Join-Path $SourceRoot "uv.lock")
$InstallerText = Get-Content -Raw -LiteralPath $InstallerScript
$ProjectVersionMatch = [regex]::Match($ProjectText, '(?m)^version\s*=\s*"([^"]+)"')
$LockVersionMatch = [regex]::Match(
    $LockText,
    '(?ms)\[\[package\]\]\s*name\s*=\s*"douyin-recall"\s*version\s*=\s*"([^"]+)"'
)
$InstallerVersionMatch = [regex]::Match($InstallerText, '(?m)^#define MyAppVersion\s+"([^"]+)"')
if (-not $ProjectVersionMatch.Success -or
    -not $LockVersionMatch.Success -or
    -not $InstallerVersionMatch.Success) {
    throw "Could not read the project, lock, and installer versions before compiling."
}
$ProjectVersion = $ProjectVersionMatch.Groups[1].Value
$LockVersion = $LockVersionMatch.Groups[1].Value
$InstallerVersion = $InstallerVersionMatch.Groups[1].Value
if ($ProjectVersion -ne $LockVersion -or $ProjectVersion -ne $InstallerVersion) {
    throw "Version mismatch: pyproject.toml=$ProjectVersion, uv.lock=$LockVersion, DouyinRecall.iss=$InstallerVersion"
}
if ($env:GITHUB_REF_TYPE -eq "tag") {
    $ExpectedTag = "v$ProjectVersion"
    if ($env:GITHUB_REF_NAME -ne $ExpectedTag) {
        throw "Version tag mismatch: expected $ExpectedTag, got '$($env:GITHUB_REF_NAME)'"
    }
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$Iscc = Find-InnoSetupCompiler -ExplicitPath $InnoSetupCompiler
Write-Host "Using Inno Setup compiler: $Iscc"
Write-Host "Building from source root: $SourceRoot"
Write-Host "Output directory: packaging\windows\out"

& $Iscc "/DSourceRoot=$SourceRoot" $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path $SetupExe)) {
    throw "Expected installer was not created: $SetupExe"
}

Write-Host "Created installer: $SetupExe"

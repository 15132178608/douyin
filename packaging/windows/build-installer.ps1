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

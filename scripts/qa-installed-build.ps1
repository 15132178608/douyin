param(
    [string]$InstallerPath = "D:\codexDownload\douyin-release-v0.1.25\DouyinRecallSetup.exe",
    [string]$QaRoot = "D:\codexDownload\douyin-release-v0.1.25\installed-qa",
    [string]$ExpectedVersion = "0.1.25",
    [int]$Port = 18765
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$UninstallRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D520E24-23C6-4C2E-8C2D-7AF8A935E32F}_is1"
$DownloadRoot = [System.IO.Path]::GetFullPath("D:\codexDownload")

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Find-Uv {
    if ($env:UV_EXE -and (Test-Path -LiteralPath $env:UV_EXE)) {
        return (Resolve-Path -LiteralPath $env:UV_EXE).Path
    }
    $command = Get-Command "uv.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $userUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $userUv) {
        return (Resolve-Path -LiteralPath $userUv).Path
    }
    throw "uv.exe was not found"
}

function Invoke-Uv {
    param(
        [string]$AppRoot,
        [string[]]$Arguments
    )
    $uv = Find-Uv
    Push-Location $AppRoot
    try {
        & $uv @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "uv $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for $Url"
}

function Read-Text {
    param([string]$Url)
    $client = [System.Net.WebClient]::new()
    try {
        $bytes = $client.DownloadData($Url)
        return [System.Text.Encoding]::UTF8.GetString($bytes)
    }
    finally {
        $client.Dispose()
    }
}

function U8 {
    param([string]$Base64)
    return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Base64))
}

function Assert-Contains {
    param(
        [string]$Text,
        [string]$Needle,
        [string]$Message
    )
    if (-not $Text.Contains($Needle)) {
        throw $Message
    }
}

function Assert-NotContains {
    param(
        [string]$Text,
        [string]$Needle,
        [string]$Message
    )
    if ($Text.Contains($Needle)) {
        throw $Message
    }
}

function Assert-InstallerVersion {
    param(
        [string]$Path,
        [string]$Expected
    )
    $version = [string](Get-Item -LiteralPath $Path).VersionInfo.ProductVersion
    $version = $version.Trim()
    if (-not [string]::Equals($version, $Expected, [System.StringComparison]::Ordinal)) {
        throw "Expected installer version $Expected, got '$version': $Path"
    }
    return $version
}

function Stop-PortOwner {
    param([int]$LocalPort)
    $connections = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $processId = [int]$connection.OwningProcess
        if ($processId -gt 0) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Save-InnoRegistration {
    if (-not (Test-Path -LiteralPath $UninstallRegistryPath)) {
        return [pscustomobject]@{
            Exists = $false
            Values = @()
        }
    }

    Get-ItemProperty -LiteralPath $UninstallRegistryPath | Out-Null
    $item = Get-Item -LiteralPath $UninstallRegistryPath
    $values = @()
    foreach ($name in $item.GetValueNames()) {
        $values += [pscustomobject]@{
            Name = $name
            Value = $item.GetValue(
                $name,
                $null,
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            )
            Kind = $item.GetValueKind($name).ToString()
        }
    }

    return [pscustomobject]@{
        Exists = $true
        Values = $values
    }
}

function Convert-RegistryKindToPropertyType {
    param([string]$Kind)
    switch ($Kind) {
        "String" { return "String" }
        "ExpandString" { return "ExpandString" }
        "Binary" { return "Binary" }
        "DWord" { return "DWord" }
        "MultiString" { return "MultiString" }
        "QWord" { return "QWord" }
        default { return "String" }
    }
}

function Restore-InnoRegistration {
    param([object]$Snapshot)

    if ($null -eq $Snapshot) {
        return
    }

    if (-not $Snapshot.Exists) {
        if (Test-Path -LiteralPath $UninstallRegistryPath) {
            Remove-Item -LiteralPath $UninstallRegistryPath -Force
        }
        return
    }

    $parent = Split-Path -Parent $UninstallRegistryPath
    $leaf = Split-Path -Leaf $UninstallRegistryPath
    if (-not (Test-Path -LiteralPath $UninstallRegistryPath)) {
        New-Item -Path $parent -Name $leaf -Force | Out-Null
    }

    $savedNames = @{}
    foreach ($entry in $Snapshot.Values) {
        $savedNames[$entry.Name] = $true
    }

    $currentItem = Get-Item -LiteralPath $UninstallRegistryPath
    foreach ($name in $currentItem.GetValueNames()) {
        if (-not $savedNames.ContainsKey($name)) {
            Remove-ItemProperty -LiteralPath $UninstallRegistryPath -Name $name -ErrorAction Stop
        }
    }

    foreach ($entry in $Snapshot.Values) {
        $propertyType = Convert-RegistryKindToPropertyType -Kind $entry.Kind
        New-ItemProperty `
            -LiteralPath $UninstallRegistryPath `
            -Name $entry.Name `
            -Value $entry.Value `
            -PropertyType $propertyType `
            -Force | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer not found: $InstallerPath"
}

function Test-RegistryValueEqual {
    param(
        [object]$Left,
        [object]$Right
    )
    $leftIsArray = $Left -is [System.Array]
    $rightIsArray = $Right -is [System.Array]
    if ($leftIsArray -or $rightIsArray) {
        if (-not ($leftIsArray -and $rightIsArray)) {
            return $false
        }
        if ($Left.Count -ne $Right.Count) {
            return $false
        }
        for ($index = 0; $index -lt $Left.Count; $index++) {
            if (-not [object]::Equals($Left[$index], $Right[$index])) {
                return $false
            }
        }
        return $true
    }
    return [object]::Equals($Left, $Right)
}

function Assert-InnoRegistrationRestored {
    param([object]$Snapshot)

    $current = Save-InnoRegistration
    if ([bool]$current.Exists -ne [bool]$Snapshot.Exists) {
        throw "Inno uninstall registration existence was not restored."
    }
    if (-not $Snapshot.Exists) {
        return
    }
    if ($current.Values.Count -ne $Snapshot.Values.Count) {
        throw "Inno uninstall registration value count was not restored."
    }
    $currentByName = @{}
    foreach ($entry in $current.Values) {
        $currentByName[$entry.Name] = $entry
    }
    foreach ($expected in $Snapshot.Values) {
        if (-not $currentByName.ContainsKey($expected.Name)) {
            throw "Inno uninstall registration value '$($expected.Name)' was not restored."
        }
        $actual = $currentByName[$expected.Name]
        if ($actual.Kind -ne $expected.Kind -or
            -not (Test-RegistryValueEqual -Left $actual.Value -Right $expected.Value)) {
            throw "Inno uninstall registration value '$($expected.Name)' changed during QA."
        }
    }
}
$QaRoot = [System.IO.Path]::GetFullPath($QaRoot)
$managedPrefix = $DownloadRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
$qaRootIsManaged = $QaRoot.StartsWith($managedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
    $QaRoot.Length -gt $managedPrefix.Length
if (-not $qaRootIsManaged) {
    throw "QaRoot must stay under D:\codexDownload: $QaRoot"
}
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$installerVersion = Assert-InstallerVersion -Path $InstallerPath -Expected $ExpectedVersion

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$appRoot = Join-Path $QaRoot "DouyinRecall-$stamp"
$reportPath = Join-Path $QaRoot "installed-qa-$stamp.json"
$installLogPath = Join-Path $QaRoot "install-$stamp.log"
$runtimeRoot = Join-Path $QaRoot "runtime"
$env:UV_CACHE_DIR = Join-Path $runtimeRoot "uv-cache"
$env:UV_LINK_MODE = "copy"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtimeRoot "ms-playwright"
$server = $null
$originalRegistration = Save-InnoRegistration
$registrationRestored = $false
$verificationPassed = $false

try {
New-Item -ItemType Directory -Path $QaRoot -Force | Out-Null
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
Stop-PortOwner -LocalPort $Port

Write-Step "Silent install to isolated QA directory"
$installArgs = @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/NOICONS",
    "/DIR=`"$appRoot`"",
    "/LOG=`"$installLogPath`""
)
$process = Start-Process -FilePath $InstallerPath -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "Installer failed with exit code $($process.ExitCode)"
}
if (-not (Test-Path -LiteralPath (Join-Path $appRoot "src\web\app.py"))) {
    throw "Installed app is missing expected source files: $appRoot"
}

Write-Step "Prepare installed runtime and database"
$envPath = Join-Path $appRoot ".env"
$envContent = @"
DB_PATH=data/recall.db
WEB_HOST=127.0.0.1
WEB_PORT=$Port
WEB_AUTH_REQUIRED=false
LOG_LEVEL=INFO
"@
Set-Content -Path $envPath -Value $envContent -Encoding UTF8
Invoke-Uv -AppRoot $appRoot -Arguments @("sync")
Invoke-Uv -AppRoot $appRoot -Arguments @("run", "python", "-m", "src.cli", "init-db")

Write-Step "Seed data and active jobs"
$seedCode = @'
from datetime import datetime, timedelta, timezone
import json
from src.db import get_connection

conn = get_connection()
now = datetime.now(timezone.utc)
conn.execute(
    """
    UPDATE users
    SET douyin_nickname = ?,
        douyin_unique_id = ?,
        douyin_avatar_url = ?,
        douyin_profile_updated_at = ?
    WHERE id = 'default'
    """,
    ("QA Account", "qa-user", "https://example.test/avatar.jpg", now),
)
for table, item_id, title, author, time_column in [
    ("favorites", "qa-fav-1", "QA favorite item", "QA favorite author", "favorited_at"),
    ("likes", "qa-like-1", "QA liked item", "QA like author", "liked_at"),
]:
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {table} (
            user_id, id, title, description, author, author_id,
            video_url, cover_url, duration_ms, {time_column},
            first_seen_at, last_seen_at, raw_json, is_removed, discovery_index
        ) VALUES (?, ?, ?, '', ?, '', ?, NULL, 0, ?, ?, ?, ?, 0, 1)
        """,
        ("default", item_id, title, author, f"https://example.test/{item_id}", now, now, now, json.dumps({})),
    )
jobs = [
    ("sync_favorites", {"content_kind": "favorites"}, "pending", None),
    ("sync_likes", {"content_kind": "likes"}, "running", now - timedelta(seconds=75)),
]
for kind, payload, status, started_at in jobs:
    conn.execute(
        """
        INSERT INTO job_queue (
            user_id, kind, payload_json, status, attempts, max_attempts,
            created_at, started_at
        ) VALUES (?, ?, ?, ?, ?, 3, ?, ?)
        """,
        ("default", kind, json.dumps(payload, ensure_ascii=False), status, 1 if status == "running" else 0, now, started_at),
    )
'@
$seedPath = Join-Path $appRoot "data\runtime\seed-installed-qa.py"
New-Item -ItemType Directory -Path (Split-Path -Parent $seedPath) -Force | Out-Null
Set-Content -Path $seedPath -Value $seedCode -Encoding UTF8
Invoke-Uv -AppRoot $appRoot -Arguments @("run", "python", $seedPath)

Write-Step "Start installed web service"
$server = Start-Process -FilePath (Join-Path $appRoot ".venv\Scripts\python.exe") `
    -ArgumentList @("-m", "src.cli", "serve", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $appRoot `
    -PassThru `
    -WindowStyle Hidden
try {
    Wait-HttpOk -Url "http://127.0.0.1:$Port/" -TimeoutSeconds 60

    Write-Step "Verify favorites and likes pages"
    $favorites = Read-Text -Url "http://127.0.0.1:$Port/"
    $likes = Read-Text -Url "http://127.0.0.1:$Port/likes"
    Assert-Contains -Text $favorites -Needle (U8 "5q2j5Zyo5ZCO5Y+w5pu05paw5pS26JeP") -Message "Favorites page should use stable background update copy."
    Assert-Contains -Text $likes -Needle (U8 "5q2j5Zyo5ZCO5Y+w5pu05paw5Zac5qyi") -Message "Likes page should use stable background update copy."
    Assert-Contains -Text $favorites -Needle (U8 "5Y+v5Lul57un57ut5rWP6KeI") -Message "Favorites page should tell users they can keep browsing."
    Assert-Contains -Text $likes -Needle (U8 "5Y+v5Lul57un57ut5rWP6KeI") -Message "Likes page should tell users they can keep browsing."
    Assert-Contains -Text $favorites -Needle "work-progress-spinner" -Message "Favorites progress banner should show a local running indicator."
    Assert-Contains -Text $likes -Needle "work-progress-spinner" -Message "Likes progress banner should show a local running indicator."
    Assert-Contains -Text $favorites -Needle "work-progress-fill" -Message "Favorites progress bar should include the animated fill."
    Assert-Contains -Text $likes -Needle "work-progress-fill" -Message "Likes progress bar should include the animated fill."
    Assert-NotContains -Text $favorites -Needle 'hx-trigger="every 3s"' -Message "Favorites list should not refresh the full result area every 3 seconds."
    Assert-NotContains -Text $likes -Needle 'hx-trigger="every 3s"' -Message "Likes list should not refresh the full result area every 3 seconds."
    Assert-NotContains -Text $favorites -Needle (U8 "5q2j5Zyo5pW055CG5pS26JeP") -Message "Favorites with local items should not show empty-state wording."
    Assert-NotContains -Text $likes -Needle (U8 "5q2j5Zyo5pW055CG5Zac5qyi") -Message "Likes with local items should not show empty-state wording."

    Write-Step "Verify repeated loads stay structurally stable"
    $favoritesAgain = Read-Text -Url "http://127.0.0.1:$Port/"
    $likesAgain = Read-Text -Url "http://127.0.0.1:$Port/likes"
    Assert-Contains -Text $favoritesAgain -Needle "QA favorite item" -Message "Favorites item disappeared on repeated load."
    Assert-Contains -Text $likesAgain -Needle "QA liked item" -Message "Likes item disappeared on repeated load."

    $verificationPassed = $true
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-PortOwner -LocalPort $Port
}
}
finally {
    Stop-PortOwner -LocalPort $Port
    Restore-InnoRegistration -Snapshot $originalRegistration
    Assert-InnoRegistrationRestored -Snapshot $originalRegistration
    $registrationRestored = $true
}

if ($verificationPassed -and $registrationRestored) {
    $portReleased = -not [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $portReleased) {
        throw "QA port $Port is still listening after service cleanup."
    }
    $installerItem = Get-Item -LiteralPath $InstallerPath
    $report = [ordered]@{
        status = "passed"
        installer = [ordered]@{
            path = $InstallerPath
            expected_version = $ExpectedVersion
            product_version = $installerVersion
            size_bytes = $installerItem.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash
        }
        app_root = $appRoot
        port = $Port
        install_log = $installLogPath
        service_stopped = $true
        port_released = $portReleased
        registration_restored = $true
    }
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Step "Installed QA passed"
    Write-Host "Installed app: $appRoot"
    Write-Host "Install log: $installLogPath"
    Write-Host "Report: $reportPath"
}

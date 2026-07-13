param(
    [ValidateSet("menu", "start", "prepare", "stop", "status", "maintenance", "auth", "diagnose", "logs", "update", "health", "repair", "backup", "backups", "restore", "verify-backup", "rollback-check")]
    [string]$Action = "menu"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # Console encoding setup is best effort; control actions should continue.
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:UV_NO_DEV = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$LogsDir = Join-Path $DataRoot "logs"
$ExportsDir = Join-Path $AppRoot "data\exports"
$ReleaseChecksDir = Join-Path $AppRoot "data\release-checks"
$EnvPath = Join-Path $AppRoot ".env"
$ProjectPath = Join-Path $AppRoot "pyproject.toml"
$ServerStatePath = Join-Path $AppRoot "data\runtime\server.json"
$ServerPidPath = Join-Path $AppRoot "data\runtime\server.pid"
$StartScript = Join-Path $ScriptDir "start-douyin-recall.ps1"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvDownloadDir = Join-Path $DownloadRoot "uv"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"
$UvInstallScriptUrl = "https://astral.sh/uv/install.ps1"
$script:CurrentPrepareStep = ""
$script:PrepareStepTotal = 5
$script:PrepareStepIndex = 0

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
    New-Item -ItemType Directory -Path $UvDownloadDir -Force | Out-Null
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

    throw "uv.exe was not found. Launch Douyin Recall once from the Start Menu, or install uv manually and retry."
}

function Find-OrInstall-Uv {
    try {
        return (Find-Uv)
    }
    catch {
        Write-Host "uv.exe was not found. Installing uv for the current Windows user."
    }

    New-Item -ItemType Directory -Path $UvDownloadDir -Force | Out-Null
    $installer = Join-Path $UvDownloadDir "install-uv.ps1"
    Write-Host "Downloading uv installer to: $installer"
    Invoke-WebRequest -Uri $UvInstallScriptUrl -OutFile $installer
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
    $installExit = $LASTEXITCODE
    if ($null -ne $installExit -and $installExit -ne 0) {
        throw "uv installer failed with exit code $installExit"
    }

    return (Find-Uv)
}

function Invoke-RecallCommand {
    param([string[]]$RecallArgs)

    Initialize-RuntimeEnvironment
    $uv = Find-Uv
    & $uv "run" "python" "-m" "src.cli" @RecallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "uv run python -m src.cli $($RecallArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-PrepareStep {
    param(
        [string]$Name,
        [string]$CommandText,
        [scriptblock]$Command,
        [switch]$LongRunning
    )

    $script:CurrentPrepareStep = "$Name ($CommandText)"
    Write-PrepareProgress -Name $Name -CommandText $CommandText -LongRunning:$LongRunning
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$CommandText failed with exit code $LASTEXITCODE"
    }
}

function Write-PrepareProgress {
    param(
        [string]$Name,
        [string]$CommandText,
        [switch]$LongRunning
    )

    $script:PrepareStepIndex += 1
    Write-Host ""
    Write-Host "Step $script:PrepareStepIndex/$script:PrepareStepTotal - $Name"
    Write-Host "Prepare step: $Name"
    Write-Host "Command: $CommandText"
    if ($LongRunning) {
        Write-Host "This step can take several minutes on first run."
    }
}

function Write-PrepareFailureHint {
    param([string]$ErrorMessage)

    $step = $script:CurrentPrepareStep
    if ([string]::IsNullOrWhiteSpace($step)) {
        $step = "Runtime environment"
    }

    $combined = "$step $ErrorMessage"
    $likely = "Runtime preparation failed during the recorded step."
    $recommended = "Retry entry: Douyin Recall Prepare Runtime. If it fails again, run uv run python -m src.cli diagnose and check the logs."

    if ($combined -like "*uv sync*") {
        $likely = "Python dependency download or virtual environment setup failed."
        $recommended = "Retry entry: Douyin Recall Prepare Runtime after checking network, proxy, and write access to the runtime cache."
    }
    elseif ($combined -like "*playwright install chromium*" -or $combined -like "*playwright*chromium*") {
        $likely = "Playwright Chromium download or browser setup failed."
        $recommended = "Retry entry: Douyin Recall Prepare Runtime after checking network access to Playwright downloads."
    }
    elseif ($combined -like "*python -m src.cli init-db*" -or $combined -like "*recall init-db*") {
        $likely = "Database initialization failed; the install or data directory may not be writable."
        $recommended = "Retry entry: Douyin Recall Prepare Runtime after checking install directory write access."
    }
    elseif ($combined -like "*uv.exe*" -or $combined -like "*uv installer*" -or $combined -like "*install-uv*") {
        $likely = "uv install or discovery failed; network, proxy, PATH, or user install permissions may be blocking it."
        $recommended = "Retry entry: Douyin Recall Prepare Runtime after checking network/proxy settings, then reopen Windows if PATH was just updated."
    }
    elseif ($combined -like "*python -m src.cli status*" -or $combined -like "*recall status*") {
        $likely = "Runtime preparation finished, but the final status check failed."
        $recommended = "Run uv run python -m src.cli diagnose, then use Douyin Recall Health Check for local state details."
    }

    Write-Host ""
    Write-Host "Prepare failed at step: $step" -ForegroundColor Yellow
    Write-Host "Likely cause: $likely"
    Write-Host "Recommended next step: $recommended"
    Write-Host "Runtime cache: $DownloadRoot"
    Write-Host "Logs: $LogsDir"
    Write-Host "Diagnostics: uv run python -m src.cli diagnose"
}

function Write-PrepareCompletionSummary {
    Write-Host ""
    Write-Host "Runtime preparation summary"
    Write-Host "Prepared steps: $script:PrepareStepIndex/$script:PrepareStepTotal"
    Write-Host "Install directory: $AppRoot"
    Write-Host "Runtime cache: $DownloadRoot"
    Write-Host "Browser cache: $PlaywrightBrowsersDir"
    Write-Host "Logs: $LogsDir"
    Write-Host "Local web service: not started by this prepare action"
    Write-Host "Next step: Use Douyin Recall to start the web UI when needed."
    Write-Host "Stop entry: Douyin Recall Stop Service"
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

function Test-DirectoryWritable {
    param(
        [string]$Name,
        [string]$Path
    )

    try {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        $ProbePath = Join-Path $Path ".douyin-recall-health-check.tmp"
        Set-Content -Path $ProbePath -Value "ok" -Encoding UTF8
        Remove-Item -LiteralPath $ProbePath -Force
        return [pscustomobject]@{
            Name = $Name
            Ok = $true
            Message = "$Name writable: $Path"
        }
    }
    catch {
        return [pscustomobject]@{
            Name = $Name
            Ok = $false
            Message = "$Name not writable: $Path. $($_.Exception.Message)"
        }
    }
}

function Test-UvAvailable {
    try {
        $uv = Find-Uv
        return [pscustomobject]@{
            Name = "uv availability"
            Ok = $true
            Message = "uv availability OK: $uv"
        }
    }
    catch {
        return [pscustomobject]@{
            Name = "uv availability"
            Ok = $false
            Message = "uv unavailable: $($_.Exception.Message)"
        }
    }
}

function Get-PortOwnerPid {
    param([int]$Port)

    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($connection) {
            return [int]$connection.OwningProcess
        }
    }
    catch {
        return $null
    }
    return $null
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

function Get-StatePid {
    param([object]$State)

    if ($null -eq $State) {
        return $null
    }

    try {
        $PidProperty = $State.PSObject.Properties["pid"]
        if ($null -eq $PidProperty -or $null -eq $PidProperty.Value) {
            return $null
        }

        return [int]$PidProperty.Value
    }
    catch {
        return $null
    }
}

function Read-ServerPidFile {
    if (-not (Test-Path $ServerPidPath)) {
        return $null
    }

    try {
        return [int]((Get-Content -Path $ServerPidPath -Raw -Encoding UTF8).Trim())
    }
    catch {
        return $null
    }
}

function Test-PidRunning {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }

    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Test-RecordedProcessRunning {
    param([object]$State)

    $ProcessId = Get-StatePid -State $State
    if ($null -eq $ProcessId) {
        return $false
    }
    return Test-PidRunning -ProcessId $ProcessId
}

function Get-ServiceAudit {
    $port = Get-WebPort
    $state = Read-ServerState
    $recordedPid = Get-StatePid -State $state
    if ($null -eq $recordedPid) {
        $recordedPid = Read-ServerPidFile
    }
    $portOwner = Get-PortOwnerPid -Port $port
    $recordedRunning = $false
    if ($null -ne $recordedPid) {
        $recordedRunning = Test-PidRunning -ProcessId $recordedPid
    }

    if ($null -eq $recordedPid) {
        if ($null -eq $portOwner) {
            return [pscustomobject]@{
                Relation = "clear"
                Action = "start"
                Port = $port
                RecordedPid = $null
                PortOwnerPid = $null
                Message = "No background web service is using port $port."
                NextStep = "Start Douyin Recall only when you need the local web UI."
            }
        }
        return [pscustomobject]@{
            Relation = "external listener"
            Action = "inspect_external"
            Port = $port
            RecordedPid = $null
            PortOwnerPid = $portOwner
            Message = "Port $port is owned by pid=$portOwner, but there is no Douyin Recall service record."
            NextStep = "Do not stop pid=$portOwner from this tool. Inspect that process or change WEB_PORT."
        }
    }

    if (-not $recordedRunning) {
        if ($null -eq $portOwner) {
            return [pscustomobject]@{
                Relation = "stale service record"
                Action = "repair"
                Port = $port
                RecordedPid = $recordedPid
                PortOwnerPid = $null
                Message = "Recorded PID $recordedPid is stale and port $port has no listener."
                NextStep = "Run Douyin Recall Repair State or uv run python -m src.cli stop to clean stale state."
            }
        }
        return [pscustomobject]@{
            Relation = "stale service record with listener"
            Action = "repair"
            Port = $port
            RecordedPid = $recordedPid
            PortOwnerPid = $portOwner
            Message = "Recorded PID $recordedPid is stale, while port $port is owned by pid=$portOwner."
            NextStep = "Run Douyin Recall Repair State or uv run python -m src.cli stop to clean project state. Do not stop pid=$portOwner unless you recognize it."
        }
    }

    if ($null -eq $portOwner) {
        return [pscustomobject]@{
            Relation = "record without listener"
            Action = "repair"
            Port = $port
            RecordedPid = $recordedPid
            PortOwnerPid = $null
            Message = "Recorded PID $recordedPid still exists, but port $port has no listener."
            NextStep = "Run Douyin Recall Repair State or uv run python -m src.cli stop, then check status again."
        }
    }

    if ([int]$portOwner -eq [int]$recordedPid) {
        return [pscustomobject]@{
            Relation = "own service running"
            Action = "stop"
            Port = $port
            RecordedPid = $recordedPid
            PortOwnerPid = $portOwner
            Message = "Douyin Recall recorded PID $recordedPid owns port $port."
            NextStep = "Run Douyin Recall Stop Service or uv run python -m src.cli stop when you are done."
        }
    }

    return [pscustomobject]@{
        Relation = "recorded PID and port owner mismatch"
        Action = "repair"
        Port = $port
        RecordedPid = $recordedPid
        PortOwnerPid = $portOwner
        Message = "Recorded PID $recordedPid does not match port $port owner pid=$portOwner."
        NextStep = "Run Douyin Recall Repair State or uv run python -m src.cli stop to clean project state. Do not stop pid=$portOwner unless you recognize it."
    }
}

function Get-ControlSummary {
    $port = Get-WebPort
    $homeUrl = "http://127.0.0.1:$port"
    $maintenanceUrl = "$homeUrl/maintenance"
    $state = Read-ServerState
    $recordedPid = $null
    $webAvailable = Test-WebAvailable -Url $homeUrl
    $serviceStatus = "stopped"

    if ($null -ne $state) {
        $recordedPid = Get-StatePid -State $state

        if (Test-RecordedProcessRunning -State $state) {
            if ($webAvailable) {
                $serviceStatus = "running"
            }
            else {
                $serviceStatus = "process exists, but local web is not responding yet"
            }
        }
        else {
            $serviceStatus = "stale PID record"
        }
    }
    elseif ($webAvailable) {
        $serviceStatus = "web reachable, but PID record is missing"
    }
    $audit = Get-ServiceAudit

    return [pscustomobject]@{
        Version = Get-InstalledVersion
        ServiceStatus = $serviceStatus
        Port = $port
        MaintenanceUrl = $maintenanceUrl
        LogsDir = $LogsDir
        DownloadRoot = $DownloadRoot
        RecordedPid = $recordedPid
        Audit = $audit
    }
}

function Write-ControlSummary {
    $summary = Get-ControlSummary

    Write-Header "Local status summary"
    Write-Host "Current version: $($summary.Version)"
    Write-Host "Service state: $($summary.ServiceStatus)"
    if ($null -ne $summary.RecordedPid) {
        Write-Host "Recorded PID: $($summary.RecordedPid)"
    }
    Write-Host "Service audit: $($summary.Audit.Relation)"
    Write-Host "Port: $($summary.Audit.Port)"
    if ($null -ne $summary.Audit.PortOwnerPid) {
        Write-Host "Port owner PID: $($summary.Audit.PortOwnerPid)"
    }
    Write-Host "Next step: $($summary.Audit.NextStep)"
    Write-Host "Maintenance: $($summary.MaintenanceUrl)"
    Write-Host "Logs: $($summary.LogsDir)"
    Write-Host "Runtime cache: $($summary.DownloadRoot)"

    if ($summary.ServiceStatus -eq "running" -or $summary.ServiceStatus -eq "process exists, but local web is not responding yet") {
        Write-Host "Stop entry: Douyin Recall Stop Service"
    }
    else {
        Write-Host "Start entry: Douyin Recall"
    }
}

function Wait-BeforeExit {
    Read-Host "Press Enter to close" | Out-Null
}

function Start-DouyinRecall {
    if (-not (Test-Path $StartScript)) {
        throw "Missing launcher script: $StartScript"
    }

    Write-Header "Start Douyin Recall"
    & $StartScript
}

function Prepare-Runtime {
    try {
        $script:PrepareStepIndex = 0
        $script:CurrentPrepareStep = "Runtime environment"
        Initialize-RuntimeEnvironment
        Write-Header "Prepare runtime"
        Write-Host "Start Menu entry: Douyin Recall Prepare Runtime"
        Write-Host "This action prepares dependencies only and does not start the local web service."
        Write-Host "Runtime cache: $DownloadRoot"
        Write-Host "Logs: $LogsDir"
        Write-Host "You can rerun this action after network or dependency download failures."

        $script:CurrentPrepareStep = "uv discovery and install"
        Write-PrepareProgress -Name "uv discovery and install" -CommandText "Find or install uv" -LongRunning
        $uv = Find-OrInstall-Uv

        Invoke-PrepareStep -Name "Python dependencies" -CommandText "uv sync" -LongRunning -Command {
            & $uv "sync" "--no-dev"
        }
        Invoke-PrepareStep -Name "Browser runtime" -CommandText "playwright install chromium" -LongRunning -Command {
            & $uv "run" "playwright" "install" "chromium"
        }
        Invoke-PrepareStep -Name "Local database" -CommandText "python -m src.cli init-db" -Command {
            & $uv "run" "python" "-m" "src.cli" "init-db"
        }
        Invoke-PrepareStep -Name "Service status" -CommandText "python -m src.cli status" -Command {
            & $uv "run" "python" "-m" "src.cli" "status"
        }

        Write-PrepareCompletionSummary
    }
    catch {
        Write-PrepareFailureHint -ErrorMessage $_.Exception.Message
        throw
    }
}

function Open-MaintenanceCenter {
    $port = Get-WebPort
    $url = "http://127.0.0.1:$port/maintenance"

    Write-Header "Open maintenance center"
    if (Test-WebAvailable -Url $url) {
        Start-Process $url
        return
    }

    Write-Host "Local web service is not responding yet. Starting it before opening maintenance."
    & $StartScript -OpenPath "/maintenance"
}

function Open-AccountRecovery {
    $port = Get-WebPort
    $url = "http://127.0.0.1:$port/auth"

    Write-Header "Open account recovery"
    Write-Host "Start Menu entry: Douyin Recall Account Recovery"
    if (Test-WebAvailable -Url $url) {
        Start-Process $url
        return
    }

    Write-Host "Local web service is not responding yet. Starting it before opening account recovery."
    & $StartScript -OpenPath "/auth"
}

function Open-LogsDirectory {
    Initialize-RuntimeEnvironment
    Write-Header "Open logs directory"
    Write-Host $LogsDir
    Start-Process $LogsDir
}

function Show-Status {
    Write-ControlSummary
    Write-Header "Service status"
    Invoke-RecallCommand @('status')
    $port = Get-WebPort
    Write-Host ""
    Write-Host "Maintenance: http://127.0.0.1:$port/maintenance"
    Write-Host "Logs: $LogsDir"
}

function Stop-DouyinRecall {
    Write-Header "Stop local web service"
    Invoke-RecallCommand @('stop')
}

function Export-Diagnostics {
    Write-Header "Export diagnostics"
    Invoke-RecallCommand @('diagnose')
}

function Check-Update {
    Write-Header "Check update"
    Invoke-RecallCommand @('update')
}

function Create-SqliteBackup {
    Initialize-RuntimeEnvironment
    Write-Header "Create SQLite backup"
    Write-Host "Start Menu entry: Douyin Recall Backup Now"
    New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
    Invoke-RecallCommand @('export', '--format', 'sqlite', '--output', $ExportsDir)
    Write-Host ""
    Write-Host "Backups directory: $ExportsDir"
}

function Open-BackupsDirectory {
    Initialize-RuntimeEnvironment
    Write-Header "Open backups directory"
    Write-Host "Start Menu entry: Douyin Recall Backups"
    New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
    Write-Host $ExportsDir
    Start-Process $ExportsDir
}

function Open-RestoreCenter {
    $port = Get-WebPort
    $url = "http://127.0.0.1:$port/maintenance"

    Write-Header "Open restore center"
    Write-Host "Start Menu entry: Douyin Recall Restore Center"
    if (Test-WebAvailable -Url $url) {
        Start-Process $url
        return
    }

    Write-Host "Local web service is not responding yet. Starting it before opening restore center."
    & $StartScript -OpenPath "/maintenance"
}

function Verify-LatestBackup {
    Initialize-RuntimeEnvironment
    Write-Header "Verify latest backup"
    Write-Host "Start Menu entry: Douyin Recall Verify Backup"
    New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
    Invoke-RecallCommand @('verify-backup', '--output', $ExportsDir)
}

function Find-LatestDeliveryManifest {
    if (-not (Test-Path $ReleaseChecksDir)) {
        throw "No release checks directory found: $ReleaseChecksDir"
    }

    $manifest = Get-ChildItem -Path $ReleaseChecksDir -Filter "delivery-manifest-*.json" -File |
        Sort-Object LastWriteTime, Name -Descending |
        Select-Object -First 1
    if ($null -eq $manifest) {
        throw "No delivery-manifest-*.json found in $ReleaseChecksDir"
    }
    return $manifest.FullName
}

function Test-ManifestRollback {
    Initialize-RuntimeEnvironment
    Write-Header "Verify delivery manifest rollback"
    Write-Host "Start Menu entry: Douyin Recall Rollback Check"
    $ManifestPath = Find-LatestDeliveryManifest
    Write-Host "Delivery manifest: $ManifestPath"
    Write-Host "This is a dry-run check only. It does not restore the database."
    Invoke-RecallCommand @('rollback-from-manifest', '--manifest', $ManifestPath, '--json')
}

function Invoke-HealthCheck {
    Write-ControlSummary
    Write-Header "Health check"
    Write-Host "Start Menu entry: Douyin Recall Health Check"

    $checks = @(
        (Test-DirectoryWritable -Name "Install directory" -Path $AppRoot),
        (Test-DirectoryWritable -Name "Logs directory" -Path $LogsDir),
        (Test-DirectoryWritable -Name "Runtime cache" -Path $DownloadRoot),
        (Test-UvAvailable)
    )

    foreach ($check in $checks) {
        $prefix = if ($check.Ok) { "[OK]" } else { "[WARN]" }
        Write-Host "$prefix $($check.Message)"
    }

    $state = Read-ServerState
    $port = Get-WebPort
    $portOwner = Get-PortOwnerPid -Port $port
    $audit = Get-ServiceAudit
    $needsRepair = $audit.Action -eq "repair"

    if ($null -eq $state) {
        Write-Host "[OK] Service record: no server.json record."
    }
    elseif (Test-RecordedProcessRunning -State $state) {
        Write-Host "[OK] Service record: recorded PID still exists."
    }
    else {
        $needsRepair = $true
        Write-Host "[WARN] Service record: stale PID record found."
    }

    if ($null -eq $portOwner) {
        Write-Host "[OK] Port listener: $port has no listener."
    }
    else {
        Write-Host "[INFO] Port listener: $port is owned by pid=$portOwner."
    }
    Write-Host "[INFO] Service audit: $($audit.Relation)"
    if ($null -ne $audit.RecordedPid) {
        Write-Host "[INFO] Recorded PID: $($audit.RecordedPid)"
    }
    if ($null -ne $audit.PortOwnerPid) {
        Write-Host "[INFO] Port owner PID: $($audit.PortOwnerPid)"
    }
    Write-Host "[INFO] Next step: $($audit.NextStep)"

    Write-Host ""
    Write-Host "Repair suggestion:"
    if ($needsRepair) {
        Write-Host "  Run: Douyin Recall Repair State"
        Write-Host "  Or run from install dir: powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\control-douyin-recall.ps1 -Action repair"
    }
    else {
        Write-Host "  No stale service record needs automatic cleanup."
    }
}

function Repair-StaleServerState {
    Write-ControlSummary
    Write-Header "Repair stale service state"
    Write-Host "Start Menu entry: Douyin Recall Repair State"

    $state = Read-ServerState
    $recordedPid = Get-StatePid -State $state
    if ($null -eq $recordedPid) {
        $recordedPid = Read-ServerPidFile
    }

    if ($null -ne $recordedPid -and (Test-PidRunning -ProcessId $recordedPid)) {
        Write-Host "Recorded service process is still running (pid=$recordedPid). State files were not cleaned. Use Douyin Recall Stop Service first."
        return
    }

    $removed = $false
    if (Test-Path $ServerStatePath) {
        Remove-Item -LiteralPath $ServerStatePath -Force
        Write-Host "Removed stale service record: $ServerStatePath"
        $removed = $true
    }
    if (Test-Path $ServerPidPath) {
        Remove-Item -LiteralPath $ServerPidPath -Force
        Write-Host "Removed stale PID file: $ServerPidPath"
        $removed = $true
    }

    if (-not $removed) {
        Write-Host "No server.json or server.pid file needed cleanup."
    }
}

function Show-ControlMenu {
    Write-ControlSummary

    while ($true) {
        Write-Host ""
        Write-Host "Douyin Recall Control"
        Write-Host "1. Start and open Web"
        Write-Host "2. Prepare runtime only"
        Write-Host "3. Open maintenance center"
        Write-Host "4. Show service status"
        Write-Host "5. Stop local web service"
        Write-Host "6. Export diagnostics"
        Write-Host "7. Open logs directory"
        Write-Host "8. Check update"
        Write-Host "9. Run health check"
        Write-Host "10. Repair stale service state"
        Write-Host "11. Create SQLite backup"
        Write-Host "12. Open backups directory"
        Write-Host "13. Open restore center"
        Write-Host "14. Verify latest backup"
        Write-Host "15. Open account recovery"
        Write-Host "16. Verify rollback manifest"
        Write-Host "0. Exit"
        $choice = Read-Host "Choose"

        switch ($choice) {
            "1" { Start-DouyinRecall; return }
            "2" { Prepare-Runtime; Wait-BeforeExit; return }
            "3" { Open-MaintenanceCenter; return }
            "4" { Show-Status; Wait-BeforeExit; return }
            "5" { Stop-DouyinRecall; Wait-BeforeExit; return }
            "6" { Export-Diagnostics; Wait-BeforeExit; return }
            "7" { Open-LogsDirectory; return }
            "8" { Check-Update; Wait-BeforeExit; return }
            "9" { Invoke-HealthCheck; Wait-BeforeExit; return }
            "10" { Repair-StaleServerState; Wait-BeforeExit; return }
            "11" { Create-SqliteBackup; Wait-BeforeExit; return }
            "12" { Open-BackupsDirectory; return }
            "13" { Open-RestoreCenter; return }
            "14" { Verify-LatestBackup; Wait-BeforeExit; return }
            "15" { Open-AccountRecovery; return }
            "16" { Test-ManifestRollback; Wait-BeforeExit; return }
            "0" { return }
            default { Write-Host "Invalid choice. Try again." -ForegroundColor Yellow }
        }
    }
}

try {
    switch ($Action) {
        "menu" { Show-ControlMenu }
        "start" { Start-DouyinRecall }
        "prepare" { Prepare-Runtime; Wait-BeforeExit }
        "maintenance" { Open-MaintenanceCenter }
        "auth" { Open-AccountRecovery }
        "status" { Show-Status; Wait-BeforeExit }
        "stop" { Stop-DouyinRecall; Wait-BeforeExit }
        "diagnose" { Export-Diagnostics; Wait-BeforeExit }
        "logs" { Open-LogsDirectory }
        "update" { Check-Update; Wait-BeforeExit }
        "health" { Invoke-HealthCheck; Wait-BeforeExit }
        "repair" { Repair-StaleServerState; Wait-BeforeExit }
        "backup" { Create-SqliteBackup; Wait-BeforeExit }
        "backups" { Open-BackupsDirectory }
        "restore" { Open-RestoreCenter }
        "verify-backup" { Verify-LatestBackup; Wait-BeforeExit }
        "rollback-check" { Test-ManifestRollback; Wait-BeforeExit }
    }
}
catch {
    Write-Host ""
    Write-Host "Douyin Recall Control failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Install directory: $AppRoot"
    Write-Host "Logs directory: $LogsDir"
    Write-Host "Runtime cache: $DownloadRoot"
    Wait-BeforeExit
    exit 1
}

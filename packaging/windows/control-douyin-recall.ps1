param(
    [ValidateSet("menu", "start", "prepare", "stop", "status", "maintenance", "auth", "diagnose", "logs", "update", "health", "repair", "backup", "backups", "restore", "verify-backup", "rollback-check")]
    [string]$Action = "menu",
    [switch]$NonInteractive
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
$env:NO_COLOR = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$DataRoot = Join-Path $AppRoot "data"
$LogsDir = Join-Path $DataRoot "logs"
$RuntimeDir = Join-Path $DataRoot "runtime"
$ExportsDir = Join-Path $AppRoot "data\exports"
$ReleaseChecksDir = Join-Path $AppRoot "data\release-checks"
$EnvPath = Join-Path $AppRoot ".env"
$EnvExamplePath = Join-Path $AppRoot ".env.example"
$ProjectPath = Join-Path $AppRoot "pyproject.toml"
$ServerStatePath = Join-Path $AppRoot "data\runtime\server.json"
$ServerPidPath = Join-Path $AppRoot "data\runtime\server.pid"
$StartScript = Join-Path $ScriptDir "start-douyin-recall.ps1"
$RuntimeCommonScript = Join-Path $ScriptDir "runtime-preparation-common.ps1"
$RuntimePreparedPath = Join-Path $RuntimeDir "runtime-prepared.json"
$PreparationStatePath = Join-Path $RuntimeDir "runtime-preparation.json"
$PreparationLockPath = Join-Path $RuntimeDir "runtime-preparation.lock"
$PrepareLog = Join-Path $LogsDir "prepare-runtime.log"
$PyProjectPath = Join-Path $AppRoot "pyproject.toml"
$UvLockPath = Join-Path $AppRoot "uv.lock"
$VenvPython = Join-Path $AppRoot ".venv\Scripts\python.exe"
$PlaywrightBrowsersJsonPath = Join-Path $AppRoot ".venv\Lib\site-packages\playwright\driver\package\browsers.json"
$DownloadRoot = "D:\codexDownload\douyinclaude-runtime"
$UvDownloadDir = Join-Path $DownloadRoot "uv"
$UvCacheDir = Join-Path $DownloadRoot "uv-cache"
$PlaywrightBrowsersDir = Join-Path $DownloadRoot "ms-playwright"
$UvInstallScriptUrl = "https://astral.sh/uv/install.ps1"
if (-not (Test-Path -LiteralPath $RuntimeCommonScript)) {
    throw "Missing runtime preparation helper: $RuntimeCommonScript"
}
. $RuntimeCommonScript
$script:CurrentPrepareStep = ""
$script:CurrentPrepareStepKey = ""
$script:PrepareStepTotal = 5
$script:PrepareStepIndex = 0
$script:PrepareStartedAt = Get-Date
$script:PreparationLockStream = $null
$script:PreparationBusy = $false
$script:CompletedPrepareSteps = @()
$script:PreparationJobHandle = [IntPtr]::Zero

function Write-Header {
    param([string]$Title)

    Write-Host ""
    Write-Host "==> $Title"
}

function Initialize-RuntimeEnvironment {
    Set-Location $AppRoot
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $UvDownloadDir -Force | Out-Null
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightBrowsersDir -Force | Out-Null
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:UV_LINK_MODE = "copy"
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
}

function Write-PrepareLog {
    param([string]$Message)

    try {
        New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
        $timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
        Add-Content -LiteralPath $PrepareLog -Value "[$timestamp] $Message" -Encoding UTF8
    }
    catch {
        # Preparation logging must not replace the original failure.
    }
}

function Write-PrepareStateBestEffort {
    param(
        [string]$Status,
        [string]$Summary,
        [string]$Detail = "",
        [string]$ErrorSummary = "",
        [string]$RecommendedAction = ""
    )

    try {
        Write-RecallPreparationState `
            -Path $PreparationStatePath `
            -Status $Status `
            -StepKey $script:CurrentPrepareStepKey `
            -StepLabel $script:CurrentPrepareStep `
            -StepIndex $script:PrepareStepIndex `
            -StepTotal $script:PrepareStepTotal `
            -Summary $Summary `
            -Detail $Detail `
            -ErrorSummary $ErrorSummary `
            -RecommendedAction $RecommendedAction `
            -StartedAt $script:PrepareStartedAt `
            -CompletedSteps $script:CompletedPrepareSteps
    }
    catch {
        Write-PrepareLog "Could not persist runtime preparation state: $($_.Exception.Message)"
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
        [string]$Key,
        [string]$Name,
        [string]$CommandText,
        [scriptblock]$Command,
        [switch]$LongRunning
    )

    $script:CurrentPrepareStep = "$Name ($CommandText)"
    $script:CurrentPrepareStepKey = $Key
    Write-PrepareProgress -Key $Key -Name $Name -CommandText $CommandText -LongRunning:$LongRunning
    & $Command 2>&1 | ForEach-Object {
        $line = [string]$_
        Write-Output $line
        Write-PrepareLog $line
    }
    $exitCode = $LASTEXITCODE
    if ($null -ne $exitCode -and $exitCode -ne 0) {
        throw "$CommandText failed with exit code $exitCode"
    }
    Write-PrepareProtocol -Event "DONE" -Key $Key -Label $Name
    Write-PrepareStateBestEffort -Status "running" -Summary "$Name completed" -Detail $CommandText
}

function Write-PrepareProgress {
    param(
        [string]$Key,
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
    Write-PrepareProtocol -Event "BEGIN" -Key $Key -Label $Name
    Write-PrepareStateBestEffort -Status "running" -Summary $Name -Detail $CommandText
}

function Write-PrepareProtocol {
    param(
        [string]$Event,
        [string]$Key,
        [string]$Label
    )

    if ($Event -in @("DONE", "SKIP") -and $Key -notin $script:CompletedPrepareSteps) {
        $script:CompletedPrepareSteps += $Key
    }
    $safeKey = ($Key -replace '\|', '/')
    $safeLabel = ($Label -replace '\|', '/')
    $line = "DR_PROGRESS|$Event|$script:PrepareStepIndex|$script:PrepareStepTotal|$safeKey|$safeLabel"
    Write-Output $line
    Write-PrepareLog $line
}

function Write-SkippedPrepareStep {
    param(
        [string]$Key,
        [string]$Name,
        [string]$Reason
    )

    $script:CurrentPrepareStep = "$Name ($Reason)"
    $script:CurrentPrepareStepKey = $Key
    Write-PrepareProgress -Key $Key -Name $Name -CommandText $Reason
    Write-PrepareProtocol -Event "SKIP" -Key $Key -Label $Name
    Write-PrepareStateBestEffort -Status "running" -Summary "$Name already ready" -Detail $Reason
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
    Write-PrepareLog "Prepare failed at step: $step"
    Write-PrepareLog "Likely cause: $likely"
    Write-PrepareLog "Recommended next step: $recommended"
    Write-PrepareLog "Error summary: $ErrorMessage"
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
            Action = "stop"
            Port = $port
            RecordedPid = $recordedPid
            PortOwnerPid = $null
            Message = "Recorded PID $recordedPid still exists, but port $port has no listener."
            NextStep = "If the service just started, wait and check again. Otherwise run Douyin Recall Stop Service or uv run python -m src.cli stop to safely recheck and clean project state."
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
        Action = "stop"
        Port = $port
        RecordedPid = $recordedPid
        PortOwnerPid = $portOwner
        Message = "Recorded PID $recordedPid does not match port $port owner pid=$portOwner."
        NextStep = "Run Douyin Recall Stop Service or uv run python -m src.cli stop to safely recheck and clean project state. Do not stop pid=$portOwner unless you recognize it."
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
    $preparationState = $null
    if (Test-Path -LiteralPath $PreparationStatePath) {
        try {
            $preparationState = Get-Content -LiteralPath $PreparationStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            Write-PrepareLog "Could not read runtime preparation state: $($_.Exception.Message)"
        }
    }

    return [pscustomobject]@{
        Version = Get-InstalledVersion
        ServiceStatus = $serviceStatus
        Port = $port
        MaintenanceUrl = $maintenanceUrl
        LogsDir = $LogsDir
        DownloadRoot = $DownloadRoot
        RecordedPid = $recordedPid
        Audit = $audit
        PreparationState = $preparationState
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
    if ($null -ne $summary.PreparationState) {
        Write-Host "Last runtime preparation: $($summary.PreparationState.status)"
        if ($summary.PreparationState.current_step) {
            Write-Host "Preparation stage: $($summary.PreparationState.current_step)"
        }
        if ($summary.PreparationState.updated_at) {
            Write-Host "Preparation updated: $($summary.PreparationState.updated_at)"
        }
        if ($summary.PreparationState.status -eq "failed") {
            Write-Host "Preparation retry: Douyin Recall Prepare Runtime"
        }
    }

    if ($summary.ServiceStatus -eq "running" -or $summary.ServiceStatus -eq "process exists, but local web is not responding yet") {
        Write-Host "Stop entry: Douyin Recall Stop Service"
    }
    else {
        Write-Host "Start entry: Douyin Recall"
    }
}

function Wait-BeforeExit {
    if ($NonInteractive) {
        return
    }
    Read-Host "Press Enter to close" | Out-Null
}

function Start-DouyinRecall {
    if (-not (Test-Path $StartScript)) {
        throw "Missing launcher script: $StartScript"
    }

    Write-Header "Start Douyin Recall"
    & $StartScript
}

function Test-PythonEnvironmentCurrent {
    param([string]$UvPath)

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        return $false
    }
    & $UvPath "sync" "--check" "--no-dev" "--color" "never" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Prepare-Runtime {
    $script:PreparationLockStream = $null
    $script:PreparationBusy = $false
    $script:CompletedPrepareSteps = @()
    try {
        $script:PrepareStepIndex = 0
        $script:CurrentPrepareStep = "Runtime environment"
        $script:CurrentPrepareStepKey = "environment"
        $script:PrepareStartedAt = Get-Date
        Initialize-RuntimeEnvironment
        $script:PreparationLockStream = Enter-RecallPreparationLock -Path $PreparationLockPath
        if ($null -eq $script:PreparationLockStream) {
            $busyMessage = "Another runtime preparation is already running. Wait for it to finish, then retry if needed. State: $PreparationStatePath"
            Write-PrepareProtocol -Event "BUSY" -Key "environment" -Label "Runtime preparation already running"
            Write-Host $busyMessage -ForegroundColor Yellow
            Write-PrepareLog $busyMessage
            $script:PreparationBusy = $true
            throw $busyMessage
        }
        $script:PreparationJobHandle = New-RecallKillOnCloseJob
        $currentProcess = [Diagnostics.Process]::GetCurrentProcess()
        Add-RecallProcessToJob `
            -JobHandle $script:PreparationJobHandle `
            -ProcessHandle $currentProcess.Handle
        Write-PrepareLog "Attached Prepare Runtime to a kill-on-close process job."
        if (Test-Path -LiteralPath $RuntimePreparedPath) {
            Remove-Item -LiteralPath $RuntimePreparedPath -Force
            Write-PrepareLog "Invalidated the previous runtime-prepared marker before preparation."
        }
        if (-not (Test-Path -LiteralPath $EnvPath)) {
            if (-not (Test-Path -LiteralPath $EnvExamplePath)) {
                throw "Missing .env.example in $AppRoot"
            }
            Copy-Item -LiteralPath $EnvExamplePath -Destination $EnvPath
        }
        Write-Header "Prepare runtime"
        Write-Host "Start Menu entry: Douyin Recall Prepare Runtime"
        Write-Host "This action prepares dependencies only and does not start the local web service."
        Write-Host "Runtime cache: $DownloadRoot"
        Write-Host "Logs: $LogsDir"
        Write-Host "You can rerun this action after network or dependency download failures."

        $script:CurrentPrepareStep = "uv discovery and install"
        $script:CurrentPrepareStepKey = "uv"
        Write-PrepareProgress -Key "uv" -Name "uv discovery and install" -CommandText "Find or install uv" -LongRunning
        $uv = Find-OrInstall-Uv
        Write-PrepareProtocol -Event "DONE" -Key "uv" -Label "uv discovery and install"

        if (Test-PythonEnvironmentCurrent -UvPath $uv) {
            Write-SkippedPrepareStep -Key "python" -Name "Python dependencies" -Reason "uv sync --check reports the environment is current"
        }
        else {
            Invoke-PrepareStep -Key "python" -Name "Python dependencies" -CommandText "uv sync" -LongRunning -Command {
                & $uv "sync" "--no-dev" "--color" "never"
            }
        }

        if (Test-RecallPlaywrightChromiumReady `
            -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
            -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath) {
            Write-SkippedPrepareStep -Key "browser" -Name "Browser runtime" -Reason "Playwright Chromium is already present"
        }
        else {
            Invoke-PrepareStep -Key "browser" -Name "Browser runtime" -CommandText "playwright install chromium" -LongRunning -Command {
                & $uv "run" "playwright" "install" "chromium"
                $installExitCode = $LASTEXITCODE
                if ($installExitCode -eq 0 -and -not (Test-RecallPlaywrightChromiumReady `
                    -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
                    -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath)) {
                    Write-Output "Browser post-install validation was incomplete; retrying with playwright install --force chromium."
                    & $uv "run" "playwright" "install" "--force" "chromium"
                }
                if (-not (Test-RecallPlaywrightChromiumReady `
                    -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
                    -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath)) {
                    throw "playwright install chromium completed, but the required browser components failed exact post-install validation"
                }
            }
        }

        Invoke-PrepareStep -Key "database" -Name "Local database" -CommandText "python -m src.cli init-db" -Command {
            & $uv "run" "python" "-m" "src.cli" "init-db"
        }
        Invoke-PrepareStep -Key "status" -Name "Service status" -CommandText "python -m src.cli status" -Command {
            & $uv "run" "python" "-m" "src.cli" "status"
        }

        Write-RecallRuntimePreparedMarker `
            -RuntimePreparedPath $RuntimePreparedPath `
            -PyProjectPath $PyProjectPath `
            -UvLockPath $UvLockPath
        Write-PrepareStateBestEffort -Status "ready" -Summary "Runtime preparation completed" -Detail "Dependencies, Chromium, and the local database are ready."
        Write-PrepareProtocol -Event "COMPLETE" -Key "complete" -Label "Runtime preparation completed"
        Write-PrepareCompletionSummary
    }
    catch {
        if ($script:PreparationBusy) {
            throw
        }
        Write-PrepareProtocol -Event "FAILED" -Key $script:CurrentPrepareStepKey -Label $script:CurrentPrepareStep
        Write-PrepareFailureHint -ErrorMessage $_.Exception.Message
        Write-PrepareStateBestEffort -Status "failed" -Summary "Runtime preparation failed" -Detail $script:CurrentPrepareStep -ErrorSummary $_.Exception.Message -RecommendedAction "Retry Douyin Recall Prepare Runtime after addressing the reported cause."
        throw
    }
    finally {
        Exit-RecallPreparationLock -LockStream $script:PreparationLockStream
        $script:PreparationLockStream = $null
        # Do not close PreparationJobHandle here: this process belongs to the job.
        # Windows closes the handle on process exit, killing only unexpected descendants.
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
    Write-Host "Suggested action:"
    if ($needsRepair) {
        Write-Host "  Run: Douyin Recall Repair State"
        Write-Host "  Or run from install dir: powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\control-douyin-recall.ps1 -Action repair"
    }
    elseif ($audit.Action -eq "stop") {
        Write-Host "  Run: Douyin Recall Stop Service"
        Write-Host "  Or run from install dir: powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\control-douyin-recall.ps1 -Action stop"
    }
    else {
        Write-Host "  Follow the next step shown above."
    }
}

function Repair-StaleServerState {
    Write-ControlSummary
    Write-Header "Repair stale service state"
    Write-Host "Start Menu entry: Douyin Recall Repair State"
    Invoke-RecallCommand @('repair-state')
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

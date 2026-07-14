param(
    [Parameter(Mandatory = $true)]
    [string]$SpecPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
$runnerExitCode = -1

function Test-RunnerOwnerAlive {
    try {
        $owner = Get-Process -Id ([int]$spec.owner_pid) -ErrorAction Stop
        $actualStartedAtTicks = $owner.StartTime.ToUniversalTime().Ticks
        $expectedStartedAtTicks = [long]::Parse(
            [string]$spec.owner_started_at_ticks,
            [Globalization.CultureInfo]::InvariantCulture
        )
        return ($actualStartedAtTicks -eq $expectedStartedAtTicks)
    }
    catch {
        return $false
    }
}

try {
    while (-not (Test-Path -LiteralPath ([string]$spec.start_gate_path) -PathType Leaf)) {
        if (-not (Test-RunnerOwnerAlive)) {
            $runnerExitCode = -3
            throw "Runtime tool owner exited before the runner start gate opened."
        }
        Start-Sleep -Milliseconds 50
    }

    $workerArguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$([string]$spec.worker_script_path)`"",
        "-SpecPath",
        "`"$([string]$spec.worker_spec_path)`""
    )
    $child = Start-Process `
        -FilePath ([string]$spec.powershell_path) `
        -ArgumentList $workerArguments `
        -WindowStyle Hidden `
        -PassThru

    $childStartedAtTicks = $child.StartTime.ToUniversalTime().Ticks
    $childPath = ""
    try {
        $childPath = [string]$child.Path
    }
    catch {
        # PID plus exact start time remains the required identity check.
    }
    $identity = [ordered]@{
        pid = [int]$child.Id
        started_at_ticks = $childStartedAtTicks.ToString([Globalization.CultureInfo]::InvariantCulture)
        executable_path = $childPath
    } | ConvertTo-Json -Compress
    $identityTempPath = "$([string]$spec.child_identity_path).$PID.tmp"
    [IO.File]::WriteAllText(
        $identityTempPath,
        $identity,
        (New-Object Text.UTF8Encoding($false))
    )
    [IO.File]::Move($identityTempPath, [string]$spec.child_identity_path)

    while (-not $child.HasExited) {
        if (-not (Test-RunnerOwnerAlive)) {
            $runnerExitCode = -2
            throw "Runtime tool owner exited; the launcher job will reclaim the child process tree."
        }
        Start-Sleep -Milliseconds 250
    }

    $child.WaitForExit()
    if (-not (Test-Path -LiteralPath ([string]$spec.tool_exit_code_path) -PathType Leaf)) {
        throw "Runtime tool worker exited without writing its exit-code sidecar."
    }
    $toolExitCodeText = (
        Get-Content -LiteralPath ([string]$spec.tool_exit_code_path) -Raw -Encoding UTF8
    ).Trim()
    $parsedToolExitCode = 0
    if (-not [int]::TryParse($toolExitCodeText, [ref]$parsedToolExitCode)) {
        throw "Runtime tool worker wrote an invalid exit code: $toolExitCodeText"
    }
    $runnerExitCode = $parsedToolExitCode
}
catch {
    $message = $_.Exception.ToString() + [Environment]::NewLine
    [IO.File]::AppendAllText(
        [string]$spec.stderr_path,
        $message,
        (New-Object Text.UTF8Encoding($false))
    )
}
finally {
    [IO.File]::WriteAllText(
        [string]$spec.exit_code_path,
        [string]$runnerExitCode,
        (New-Object Text.UTF8Encoding($false))
    )
}

exit $runnerExitCode

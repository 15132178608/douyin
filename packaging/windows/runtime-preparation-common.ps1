function Get-RecallFileSha256 {
    param([string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha256.ComputeHash($stream)
            return ([System.BitConverter]::ToString($bytes)).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Get-RecallRuntimeFingerprint {
    param(
        [string]$PyProjectPath,
        [string]$UvLockPath
    )

    $parts = @()
    foreach ($path in @($PyProjectPath, $UvLockPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Runtime fingerprint input is missing: $path"
        }
        $hash = Get-RecallFileSha256 -Path $path
        if ([string]::IsNullOrWhiteSpace($hash)) {
            throw "Runtime fingerprint input could not be hashed: $path"
        }
        $parts += "$(Split-Path -Leaf $path)=$hash"
    }
    return ($parts -join "|")
}

function Test-RecallPlaywrightChromiumReady {
    param(
        [string]$PlaywrightBrowsersDir,
        [string]$PlaywrightBrowsersJsonPath
    )

    if (-not (Test-Path -LiteralPath $PlaywrightBrowsersDir -PathType Container)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $PlaywrightBrowsersJsonPath -PathType Leaf)) {
        return $false
    }
    try {
        $manifest = Get-Content -LiteralPath $PlaywrightBrowsersJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($requiredName in @("chromium", "chromium-headless-shell", "ffmpeg", "winldd")) {
            $matches = @($manifest.browsers | Where-Object { $_.name -eq $requiredName })
            if ($matches.Count -ne 1) {
                return $false
            }
            $browser = $matches[0]
            $revision = [string]$browser.revision
            if ([string]::IsNullOrWhiteSpace($revision)) {
                return $false
            }
            if ($requiredName -eq "chromium") {
                $browserDir = Join-Path $PlaywrightBrowsersDir "chromium-$revision"
                $candidates = @("chrome-win\chrome.exe", "chrome-win64\chrome.exe")
            }
            elseif ($requiredName -eq "chromium-headless-shell") {
                $browserDir = Join-Path $PlaywrightBrowsersDir "chromium_headless_shell-$revision"
                $candidates = @(
                    "chrome-headless-shell-win\chrome-headless-shell.exe",
                    "chrome-headless-shell-win64\chrome-headless-shell.exe"
                )
            }
            elseif ($requiredName -eq "ffmpeg") {
                $browserDir = Join-Path $PlaywrightBrowsersDir "ffmpeg-$revision"
                $candidates = @("ffmpeg-win.exe", "ffmpeg-win64.exe")
            }
            else {
                $browserDir = Join-Path $PlaywrightBrowsersDir "winldd-$revision"
                $candidates = @("PrintDeps.exe")
            }
            if (-not (Test-Path -LiteralPath (Join-Path $browserDir "INSTALLATION_COMPLETE") -PathType Leaf)) {
                return $false
            }
            $found = $false
            foreach ($relativePath in $candidates) {
                if (Test-Path -LiteralPath (Join-Path $browserDir $relativePath) -PathType Leaf) {
                    $found = $true
                    break
                }
            }
            if (-not $found) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-RecallRuntimePrepared {
    param(
        [string]$RuntimePreparedPath,
        [string]$VenvPython,
        [string]$EnvPath,
        [string]$PlaywrightBrowsersDir,
        [string]$PlaywrightBrowsersJsonPath,
        [string]$PyProjectPath,
        [string]$UvLockPath
    )

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return $false
    }
    if (-not (Test-RecallPlaywrightChromiumReady `
        -PlaywrightBrowsersDir $PlaywrightBrowsersDir `
        -PlaywrightBrowsersJsonPath $PlaywrightBrowsersJsonPath)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $RuntimePreparedPath)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $RuntimePreparedPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $fingerprint = Get-RecallRuntimeFingerprint -PyProjectPath $PyProjectPath -UvLockPath $UvLockPath
        return (
            [int]$state.schema_version -eq 1 -and
            [string]$state.fingerprint -eq $fingerprint
        )
    }
    catch {
        return $false
    }
}

function Write-RecallJsonAtomic {
    param(
        [string]$Path,
        [object]$Value
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $tempPath = "$Path.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    $backupPath = "$tempPath.bak"
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tempPath -Encoding UTF8
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($tempPath, $Path, $backupPath, $true)
        }
        else {
            [System.IO.File]::Move($tempPath, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

function Write-RecallTextAtomic {
    param(
        [string]$Path,
        [string]$Value
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $tempPath = "$Path.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    $backupPath = "$tempPath.bak"
    try {
        Set-Content -LiteralPath $tempPath -Value $Value -Encoding UTF8
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($tempPath, $Path, $backupPath, $true)
        }
        else {
            [System.IO.File]::Move($tempPath, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force
        }
        if (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

function Write-RecallRuntimePreparedMarker {
    param(
        [string]$RuntimePreparedPath,
        [string]$PyProjectPath,
        [string]$UvLockPath
    )

    $payload = [ordered]@{
        schema_version = 1
        prepared_at = (Get-Date).ToUniversalTime().ToString("o")
        fingerprint = Get-RecallRuntimeFingerprint -PyProjectPath $PyProjectPath -UvLockPath $UvLockPath
    }
    Write-RecallJsonAtomic -Path $RuntimePreparedPath -Value $payload
}

function Write-RecallPreparationState {
    param(
        [string]$Path,
        [string]$Status,
        [string]$StepKey = "",
        [string]$StepLabel = "",
        [int]$StepIndex = 0,
        [int]$StepTotal = 0,
        [string]$Summary = "",
        [string]$Detail = "",
        [string]$ErrorSummary = "",
        [string]$RecommendedAction = "",
        [datetime]$StartedAt = (Get-Date),
        [string[]]$CompletedSteps = @()
    )

    $now = Get-Date
    $payload = [ordered]@{
        schema_version = 1
        status = $Status
        current_step = $StepKey
        current_step_label = $StepLabel
        step_index = $StepIndex
        step_total = $StepTotal
        summary = $Summary
        detail = $Detail
        completed_steps = @($CompletedSteps)
        started_at = $StartedAt.ToUniversalTime().ToString("o")
        updated_at = $now.ToUniversalTime().ToString("o")
        elapsed_seconds = [int][Math]::Max(0, ($now - $StartedAt).TotalSeconds)
        error_summary = $ErrorSummary
        recommended_action = $RecommendedAction
    }
    Write-RecallJsonAtomic -Path $Path -Value $payload
}

function Enter-RecallPreparationLock {
    param([string]$Path)

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $owner = [System.Text.Encoding]::UTF8.GetBytes("pid=$PID started=$((Get-Date).ToUniversalTime().ToString('o'))")
        $stream.SetLength(0)
        $stream.Write($owner, 0, $owner.Length)
        $stream.Flush()
        return $stream
    }
    catch [System.IO.IOException] {
        return $null
    }
}

function Exit-RecallPreparationLock {
    param([object]$LockStream)

    if ($null -ne $LockStream) {
        $LockStream.Dispose()
    }
}

function Initialize-RecallRuntimeJobType {
    if ("DouyinRecall.RuntimeJob" -as [type]) {
        return
    }

    $source = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace DouyinRecall
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static class RuntimeJob
    {
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr CreateKillOnClose()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
                throw new Win32Exception(Marshal.GetLastWin32Error());

            JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(info, pointer, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, pointer, (uint)length))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return job;
            }
            catch
            {
                CloseHandle(job);
                throw;
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        public static void Assign(IntPtr job, IntPtr process)
        {
            if (!AssignProcessToJobObject(job, process))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        public static void Close(IntPtr job)
        {
            if (job != IntPtr.Zero)
                CloseHandle(job);
        }
    }
}
'@
    Add-Type -TypeDefinition $source -Language CSharp
}

function New-RecallKillOnCloseJob {
    Initialize-RecallRuntimeJobType
    return [DouyinRecall.RuntimeJob]::CreateKillOnClose()
}

function Add-RecallProcessToJob {
    param(
        [IntPtr]$JobHandle,
        [IntPtr]$ProcessHandle
    )

    [DouyinRecall.RuntimeJob]::Assign($JobHandle, $ProcessHandle)
}

function Close-RecallRuntimeJob {
    param([IntPtr]$JobHandle)

    [DouyinRecall.RuntimeJob]::Close($JobHandle)
}

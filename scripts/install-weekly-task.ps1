param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$TaskName = "DouyinRecallWeeklyMaintenance",
    [string]$DayOfWeek = "Sunday",
    [string]$At = "09:00"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$runner = Join-Path $ProjectRoot "scripts\run-weekly-maintenance.ps1"
if (-not (Test-Path $runner)) {
    throw "Cannot find $runner"
}

$time = [datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Weekly Douyin Recall crawl, index, digest, and SQLite backup." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Check it with: Get-ScheduledTask -TaskName $TaskName"

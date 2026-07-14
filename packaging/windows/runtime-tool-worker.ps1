param(
    [Parameter(Mandatory = $true)]
    [string]$SpecPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
$toolExitCode = -1

try {
    $arguments = @($spec.arguments | ForEach-Object { [string]$_ })
    $tool = Start-Process `
        -FilePath ([string]$spec.file_path) `
        -ArgumentList $arguments `
        -WorkingDirectory ([string]$spec.working_directory) `
        -WindowStyle Hidden `
        -RedirectStandardOutput ([string]$spec.stdout_path) `
        -RedirectStandardError ([string]$spec.stderr_path) `
        -Wait `
        -PassThru
    $toolExitCode = [int]$tool.ExitCode
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
        [string]$spec.tool_exit_code_path,
        [string]$toolExitCode,
        (New-Object Text.UTF8Encoding($false))
    )
}

exit $toolExitCode

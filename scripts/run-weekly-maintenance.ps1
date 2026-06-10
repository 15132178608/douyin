param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [int]$MaxPages = 500,
    [switch]$SendLikesDigest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $ProjectRoot

# Pipeline commands covered by this runner:
# uv run recall crawl
# uv run recall crawl-likes
# uv run recall index --kind favorites
# uv run recall index --kind likes
# uv run recall digest --kind favorites
# uv run recall export --format sqlite

$logDir = Join-Path $ProjectRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDir "weekly-maintenance-$stamp.log"

function Invoke-RecallStep {
    param(
        [string]$Name,
        [string[]]$Args
    )

    "[$(Get-Date -Format o)] START $Name" | Tee-Object -FilePath $logPath -Append
    & uv @Args 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
    "[$(Get-Date -Format o)] DONE  $Name" | Tee-Object -FilePath $logPath -Append
}

Invoke-RecallStep "crawl favorites" @("run", "recall", "crawl", "--max-pages", "$MaxPages")
Invoke-RecallStep "crawl likes" @("run", "recall", "crawl-likes", "--max-pages", "$MaxPages")
Invoke-RecallStep "index favorites" @("run", "recall", "index", "--kind", "favorites")
Invoke-RecallStep "index likes" @("run", "recall", "index", "--kind", "likes")
Invoke-RecallStep "digest favorites" @("run", "recall", "digest", "--kind", "favorites")
if ($SendLikesDigest) {
    Invoke-RecallStep "digest likes" @("run", "recall", "digest", "--kind", "likes")
}
Invoke-RecallStep "sqlite backup" @("run", "recall", "export", "--format", "sqlite", "--output", (Join-Path $ProjectRoot "data\exports"))

"[$(Get-Date -Format o)] WEEKLY MAINTENANCE COMPLETE" | Tee-Object -FilePath $logPath -Append

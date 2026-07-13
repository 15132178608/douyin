param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
}

$errors = @()
$result = [ordered]@{
    schema_version = 1
    ok = $false
    path = [System.IO.Path]::GetFullPath($InstallerPath)
    name = [System.IO.Path]::GetFileName($InstallerPath)
    size_bytes = 0
    sha256 = $null
    product_version = $null
    file_version = $null
    expected_version = $ExpectedVersion
    authenticode_status = $null
    errors = @()
}

try {
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "Installer not found: $InstallerPath"
    }

    $utilityModule = Join-Path $PSHOME "Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1"
    $securityModule = Join-Path $PSHOME "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
    Import-Module -Name $utilityModule -ErrorAction Stop
    Import-Module -Name $securityModule -ErrorAction Stop

    $resolvedPath = (Resolve-Path -LiteralPath $InstallerPath).Path
    $item = Get-Item -LiteralPath $resolvedPath
    $productVersion = ([string]$item.VersionInfo.ProductVersion).Trim()
    $fileVersion = ([string]$item.VersionInfo.FileVersion).Trim()
    $signature = Get-AuthenticodeSignature -FilePath $resolvedPath

    $result.path = $resolvedPath
    $result.name = $item.Name
    $result.size_bytes = [int64]$item.Length
    $result.sha256 = (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash
    $result.product_version = $productVersion
    $result.file_version = $fileVersion
    $result.authenticode_status = [string]$signature.Status

    if ($item.Length -le 0) {
        $errors += "Installer is empty."
    }
    if ([string]::IsNullOrWhiteSpace($productVersion)) {
        $errors += "Installer ProductVersion is missing."
    }
    elseif ($productVersion -ne $ExpectedVersion) {
        $errors += "Installer ProductVersion '$productVersion' does not match expected '$ExpectedVersion'."
    }
    if ([string]::IsNullOrWhiteSpace([string]$result.sha256)) {
        $errors += "Installer SHA256 could not be calculated."
    }
    if ($signature.Status -notin @(
        [System.Management.Automation.SignatureStatus]::Valid,
        [System.Management.Automation.SignatureStatus]::NotSigned
    )) {
        $errors += "Installer Authenticode status is '$($signature.Status)'."
    }
}
catch {
    $errors += $_.Exception.Message
}

$result.errors = @($errors)
$result.ok = $errors.Count -eq 0
$result | ConvertTo-Json -Depth 5 -Compress

if (-not $result.ok) {
    exit 1
}

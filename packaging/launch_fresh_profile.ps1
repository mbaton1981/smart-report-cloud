[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ExecutablePath,
    [switch]$WaitForExit
)

$ErrorActionPreference = "Stop"
if (-not $ExecutablePath) {
    $ExecutablePath = Join-Path $PSScriptRoot "SmartReport.exe"
}
$executable = Get-Item -LiteralPath $ExecutablePath
if ($executable.PSIsContainer) {
    throw "SmartReport.exe was not found."
}
foreach ($name in @(
        "SMARTREPORT_DB_FILE", "SMARTREPORT_DATA_DIR", "SMARTREPORT_EXPECTED_DATASET",
        "SMARTREPORT_CANDIDATE_PROFILE", "SMARTREPORT_TRIAL_MODE", "SYNC_API_KEY",
        "SMARTREPORT_ASSISTANT_CANDIDATE"
    )) {
    if ([Environment]::GetEnvironmentVariable($name, "Process")) {
        throw "An existing profile or test environment is configured. Use its approved launcher."
    }
}
foreach ($marker in @("paired_install.json", "candidate_install.json")) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot $marker)) {
        throw "This installation requires its paired launcher."
    }
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is unavailable; the profile was not created."
}

$profileDirectory = Join-Path $env:LOCALAPPDATA "SmartReport\default-profile"
$database = [IO.Path]::GetFullPath((Join-Path $profileDirectory "smart_report.db"))
$markerPath = Join-Path $profileDirectory "fresh_profile.json"
if (-not $PSCmdlet.ShouldProcess($database, "Start SmartReport with a new user profile")) {
    return
}
if (Test-Path -LiteralPath $markerPath) {
    $profile = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($profile.profile_type -ne "fresh" -or
        -not [string]::Equals([IO.Path]::GetFullPath([string]$profile.database), $database,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "The first-run profile marker is invalid. No database was opened."
    }
} else {
    if (Test-Path -LiteralPath $profileDirectory) {
        $existing = Get-Item -LiteralPath $profileDirectory
        if (-not $existing.PSIsContainer -or @(Get-ChildItem -LiteralPath $profileDirectory -Force).Count -gt 0) {
            throw "A profile already exists without a first-run marker. Use its approved launcher."
        }
    } else {
        New-Item -ItemType Directory -Path $profileDirectory | Out-Null
    }
    [ordered]@{ profile_type = "fresh"; database = $database } |
        ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8
}

$env:SMARTREPORT_DB_FILE = $database
$env:SMARTREPORT_DATA_DIR = Join-Path $profileDirectory "profile"
$env:SMARTREPORT_DOCUMENTS_DIR = Join-Path $profileDirectory "documents"
$env:SMARTREPORT_MAIL_SETTINGS_FILE = Join-Path $profileDirectory "mail_settings.json"
$client = Start-Process -FilePath $executable.FullName -WorkingDirectory $profileDirectory -PassThru
if ($WaitForExit) {
    Write-Output $client.Id
    Wait-Process -Id $client.Id
}

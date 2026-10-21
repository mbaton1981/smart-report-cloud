[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProfileRoot,
    [string]$ExecutablePath = (Join-Path $PSScriptRoot "SmartReport.exe")
)

$ErrorActionPreference = "Stop"
$profileDirectory = (Get-Item -LiteralPath $ProfileRoot).FullName
$database = Get-Item -LiteralPath (Join-Path $profileDirectory "smart_report.db")
$executable = Get-Item -LiteralPath $ExecutablePath
$configuration = Get-Content -LiteralPath (Join-Path $profileDirectory "working_profile.json") -Raw |
    ConvertFrom-Json
if ($database.PSIsContainer -or $executable.PSIsContainer -or $database.Length -lt 100 -or
    $configuration.approved_transition -ne $true -or
    -not $configuration.dataset_id -or
    $configuration.server_url -ne "https://smart-report-server.onrender.com") {
    throw "The approved existing profile is invalid. No data was changed."
}
$running = Get-CimInstance Win32_Process -Filter "Name = 'SmartReport.exe'"
if ($running) {
    throw "Close the current SmartReport only after saving notes and approving the switch."
}
if ($PSCmdlet.ShouldProcess($database.FullName, "Start the unified client with the existing approved profile")) {
    $env:SMARTREPORT_DB_FILE = $database.FullName
    $env:SMARTREPORT_DATA_DIR = Join-Path $profileDirectory "profile"
    $env:SMARTREPORT_DOCUMENTS_DIR = Join-Path $profileDirectory "documents"
    $env:SMARTREPORT_MAIL_SETTINGS_FILE = Join-Path $profileDirectory "mail_settings.json"
    $env:SMARTREPORT_CLOUD_URL = $configuration.server_url
    $env:SMARTREPORT_EXPECTED_DATASET = $configuration.dataset_id
    foreach ($name in @("SYNC_API_KEY", "SMARTREPORT_TRIAL_MODE", "SMARTREPORT_CANDIDATE_PROFILE")) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    Start-Process -FilePath $executable.FullName -WorkingDirectory $profileDirectory
}

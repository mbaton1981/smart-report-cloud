param(
    [string]$DataDir = "$env:LOCALAPPDATA\SmartReport-Candidate"
)
$ErrorActionPreference = "Stop"
try {
$data = [System.IO.Path]::GetFullPath($DataDir)
$db = Join-Path $data "smart_report.db"
$marker = Join-Path $data "candidate_profile.json"
if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    throw "This is not a prepared isolated candidate profile: $data"
}
$profile = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
if ($profile.isolated_candidate -ne $true -or $profile.database -ne $db -or $profile.source_database -eq $db) {
    throw "Invalid isolated candidate profile: $data"
}
if (-not (Test-Path -LiteralPath $db -PathType Leaf)) {
    throw "Candidate requires an explicitly prepared isolated database: $db"
}
$env:SMARTREPORT_DATA_DIR = Join-Path $data "profile"
$env:SMARTREPORT_DB_FILE = $db
$env:SMARTREPORT_DOCUMENTS_DIR = Join-Path $data "documents"
$env:SMARTREPORT_MAIL_SETTINGS_FILE = Join-Path $data "mail_settings.json"
$env:SMARTREPORT_CLOUD_URL = "http://127.0.0.1:5999"
$env:SMARTREPORT_TRIAL_MODE = "1"
$env:SMARTREPORT_CANDIDATE_PROFILE = $marker
$env:SMARTREPORT_CANDIDATE_VERSION = $profile.release_version
Remove-Item Env:SYNC_API_KEY -ErrorAction SilentlyContinue
Set-Location -LiteralPath $data
& (Join-Path $PSScriptRoot "SmartReport.exe")
exit $LASTEXITCODE
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $_.Exception.Message, "SmartReport - candidate startup error", "OK", "Error"
    ) | Out-Null
    throw
}

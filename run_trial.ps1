param(
    [Parameter(Mandatory = $true)]
    [string]$TrialDir
)

# Пробный запуск SmartReport на отдельной тестовой БД. Рабочая БД, архив и облако не затрагиваются.
$ErrorActionPreference = "Stop"
$project = $PSScriptRoot
$trial = [System.IO.Path]::GetFullPath($TrialDir)

if ($trial.TrimEnd('\').StartsWith($project.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Папка пробного запуска не должна находиться внутри папки проекта: $trial"
}

$python = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Не найден интерпретатор $python" }

New-Item -ItemType Directory -Force (Join-Path $trial "documents") | Out-Null
Write-Host "Пробный режим. Тестовая БД:  $trial\smart_report_test.db"
Write-Host "Папка вложений:             $trial\documents"
Write-Host "Настройки почты:            $trial\mail_settings.json"

$names = "SMARTREPORT_TRIAL_MODE", "SMARTREPORT_DB_FILE", "SMARTREPORT_DOCUMENTS_DIR",
         "SMARTREPORT_MAIL_SETTINGS_FILE", "SMARTREPORT_CLOUD_URL"
try {
    $env:SMARTREPORT_TRIAL_MODE = "1"
    $env:SMARTREPORT_DB_FILE = Join-Path $trial "smart_report_test.db"
    $env:SMARTREPORT_DOCUMENTS_DIR = Join-Path $trial "documents"
    $env:SMARTREPORT_MAIL_SETTINGS_FILE = Join-Path $trial "mail_settings.json"
    $env:SMARTREPORT_CLOUD_URL = "http://127.0.0.1:5999"
    Push-Location $trial
    & $python (Join-Path $project "app.py")
    if ($LASTEXITCODE -ne 0) { Write-Host "Приложение завершилось с кодом $LASTEXITCODE" }
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
    foreach ($name in $names) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}

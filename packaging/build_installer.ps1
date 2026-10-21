# Сборка установщика: .\packaging\build_installer.ps1 [-Version 1.0.0]
# Требуется сборочное окружение (venv с PyInstaller) и Inno Setup 6; пути можно задать параметрами.
param(
    [string]$Version = "1.0.0",
    [string]$BuildPython = "$env:LOCALAPPDATA\SmartReport\build\venv\Scripts\python.exe",
    [string]$Iscc = "$env:LOCALAPPDATA\SmartReport\build\InnoSetup6\ISCC.exe",
    [string]$OutputRoot = "$env:LOCALAPPDATA\SmartReport\build\out",
    [switch]$Candidate,
    [switch]$PairedRelease,
    [string]$CandidateSubdir = "SmartReport-Candidate",
    [string]$CandidateName = "SmartReport Candidate",
    [string]$CandidateAppId = "{{A05619D2-ABDD-4A10-9487-73DEBA6078D5}"
)
$ErrorActionPreference = "Continue"  # PyInstaller пишет прогресс в stderr; ошибки ловим по коду выхода
$root = Split-Path -Parent $PSScriptRoot
$build = $OutputRoot
New-Item -ItemType Directory -Force $build | Out-Null
$env:PYTHONIOENCODING = "utf-8"
& $BuildPython -m PyInstaller --noconfirm --clean --distpath "$build\dist" --workpath "$build\work" "$PSScriptRoot\smartreport.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
$defines = @("/DAppVersion=$Version", "/DSourceDir=$build\dist\SmartReport", "/O$build\installer")
if ($Candidate -and $PairedRelease) { throw "Candidate and PairedRelease cannot be combined." }
if ($PairedRelease) {
    $defines += @("/DPairedReleaseBuild", "/DAppDisplayName=SmartReport 1.0.9", "/DInstallSubdir=SmartReport-1.0.9",
                  "/DApplicationId={{BD418D63-34F0-481F-9B65-C627E2A2E110}")
}
if ($Candidate) {
    $defines += @("/DCandidateBuild", "/DAppDisplayName=$CandidateName", "/DInstallSubdir=$CandidateSubdir",
                  "/DApplicationId=$CandidateAppId")
}
& $Iscc @defines "$PSScriptRoot\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
Get-FileHash "$build\installer\SmartReport-Setup-$Version.exe" -Algorithm SHA256

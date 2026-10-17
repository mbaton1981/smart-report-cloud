# Сборка установщика: .\packaging\build_installer.ps1 [-Version 1.0.0]
# Требуется сборочное окружение (venv с PyInstaller) и Inno Setup 6; пути можно задать параметрами.
param(
    [string]$Version = "1.0.0",
    [string]$BuildPython = "$env:LOCALAPPDATA\SmartReport\build\venv\Scripts\python.exe",
    [string]$Iscc = "$env:LOCALAPPDATA\SmartReport\build\InnoSetup6\ISCC.exe"
)
$ErrorActionPreference = "Continue"  # PyInstaller пишет прогресс в stderr; ошибки ловим по коду выхода
$root = Split-Path -Parent $PSScriptRoot
$build = "$env:LOCALAPPDATA\SmartReport\build\out"
New-Item -ItemType Directory -Force $build | Out-Null
$env:PYTHONIOENCODING = "utf-8"
& $BuildPython -m PyInstaller --noconfirm --clean --distpath "$build\dist" --workpath "$build\work" "$PSScriptRoot\smartreport.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
& $Iscc "/DAppVersion=$Version" "/DSourceDir=$build\dist\SmartReport" "/O$build\installer" "$PSScriptRoot\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
Get-FileHash "$build\installer\SmartReport-Setup-$Version.exe" -Algorithm SHA256

# Сборка установщика: .\packaging\build_installer.ps1 [-Version 1.0.0]
# Требуется сборочное окружение (venv с PyInstaller) и Inno Setup 6; пути можно задать параметрами.
param(
    [string]$Version = "1.0.25-rc6",
    [string]$BuildPython = "$env:LOCALAPPDATA\SmartReport\build\venv\Scripts\python.exe",
    [string]$Iscc = "$env:LOCALAPPDATA\SmartReport\build\InnoSetup6\ISCC.exe",
    [string]$OutputRoot = "$env:LOCALAPPDATA\SmartReport\build\out"
)
$ErrorActionPreference = "Continue"  # PyInstaller пишет прогресс в stderr; ошибки ловим по коду выхода
$root = Split-Path -Parent $PSScriptRoot
$build = $OutputRoot
New-Item -ItemType Directory -Force $build | Out-Null
$commit = & git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "Cannot identify build source commit" }
$changes = & git -C $root status --porcelain --untracked-files=no
if ($changes) { throw "Commit tracked source changes before building" }
$manifest = Join-Path $build "release-manifest.json"
@{version=$Version; commit=$commit; built_at=(Get-Date).ToUniversalTime().ToString("o")} |
    ConvertTo-Json | Set-Content -Encoding UTF8 $manifest
$env:SMARTREPORT_BUILD_MANIFEST = $manifest
$env:PYTHONIOENCODING = "utf-8"
& $BuildPython -m PyInstaller --noconfirm --clean --distpath "$build\dist" --workpath "$build\work" "$PSScriptRoot\smartreport.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
& $Iscc "/DAppVersion=$Version" "/DSourceDir=$build\dist\SmartReport" "/O$build\installer" "$PSScriptRoot\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
Get-FileHash "$build\installer\SmartReport-Setup-$Version.exe" -Algorithm SHA256

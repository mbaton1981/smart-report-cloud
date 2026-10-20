[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$DatabasePath,
    [string]$ExecutablePath = "$env:LOCALAPPDATA\Programs\SmartReport\SmartReport.exe"
)

$ErrorActionPreference = "Stop"

try {
    $database = Get-Item -LiteralPath $DatabasePath
    $executable = Get-Item -LiteralPath $ExecutablePath
    if ($database.PSIsContainer -or $database.Length -lt 100) {
        throw "The existing SmartReport database is missing or invalid. No database was created."
    }
    if ($executable.PSIsContainer) {
        throw "SmartReport.exe is not a file."
    }
    $running = Get-CimInstance Win32_Process -Filter "Name = 'SmartReport.exe'" |
        Where-Object { $_.ExecutablePath -eq $executable.FullName }
    if ($running) {
        throw "SmartReport is already running. Close its existing window before starting another profile."
    }
    if ($PSCmdlet.ShouldProcess($database.FullName, "Start installed SmartReport with this database")) {
        $env:SMARTREPORT_DB_FILE = $database.FullName
        Start-Process -FilePath $executable.FullName -WorkingDirectory $database.DirectoryName
    }
} catch {
    if (-not $WhatIfPreference) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $_.Exception.Message, "SmartReport - startup error", "OK", "Error"
        ) | Out-Null
    }
    throw
}

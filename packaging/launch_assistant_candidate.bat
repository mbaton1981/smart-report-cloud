@echo off
setlocal
set "PROFILE=%LOCALAPPDATA%\SmartReport-Candidate-Connected-1.0.28-rc3"
set "SMARTREPORT_DB_FILE=%PROFILE%\smart_report.db"
set "SMARTREPORT_DATA_DIR=%PROFILE%\profile"
set "SMARTREPORT_DOCUMENTS_DIR=%PROFILE%\documents"
set "SMARTREPORT_CLOUD_URL=https://smart-report-server.onrender.com"
set "SMARTREPORT_ASSISTANT_CANDIDATE=1"
cd /d "%~dp0"
start "" "%~dp0SmartReport.exe"
endlocal

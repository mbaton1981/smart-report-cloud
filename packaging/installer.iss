; Установщик SmartReport: без прав администратора, в профиль пользователя. Секретов не содержит.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\SmartReport"
#endif

[Setup]
AppId=SmartReport-UnifiedDesktop
AppName=SmartReport
AppVersion={#AppVersion}
AppPublisher=SmartReport
DefaultDirName={localappdata}\Programs\SmartReport-{#AppVersion}
UsePreviousAppDir=no
DefaultGroupName=SmartReport
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=SmartReport-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\SmartReport.exe
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "shortcuts"; Description: "Create Start Menu and Desktop shortcuts"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\SmartReport {#AppVersion}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\launch_fresh_profile.ps1"" -ExecutablePath ""{app}\SmartReport.exe"""; WorkingDir: "{app}"; IconFilename: "{app}\SmartReport.exe"; Tasks: shortcuts
Name: "{autodesktop}\SmartReport {#AppVersion}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\launch_fresh_profile.ps1"" -ExecutablePath ""{app}\SmartReport.exe"""; WorkingDir: "{app}"; IconFilename: "{app}\SmartReport.exe"; Tasks: shortcuts

; Данные пользователя (%LOCALAPPDATA%\SmartReport) при удалении программы сохраняются.

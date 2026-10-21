; Установщик SmartReport: без прав администратора, в профиль пользователя. Секретов не содержит.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\SmartReport"
#endif

[Setup]
AppId=SmartReport-UnifiedCandidate
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
Source: "launch_existing_profile.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Данные пользователя (%LOCALAPPDATA%\SmartReport) при удалении программы сохраняются.

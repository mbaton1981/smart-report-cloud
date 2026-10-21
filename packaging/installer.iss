; Установщик SmartReport: без прав администратора, в профиль пользователя. Секретов не содержит.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\SmartReport"
#endif
#ifndef AppDisplayName
  #define AppDisplayName "SmartReport"
#endif
#ifndef InstallSubdir
  #define InstallSubdir "SmartReport"
#endif
#ifndef ApplicationId
  #define ApplicationId "{{6C0B6F5E-5E57-4F0B-9B34-5A7D1C0A9E11}"
#endif

[Setup]
AppId={#ApplicationId}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppPublisher=SmartReport
DefaultDirName={localappdata}\Programs\{#InstallSubdir}
DefaultGroupName={#AppDisplayName}
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=SmartReport-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\SmartReport.exe
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
#ifdef CandidateBuild
Source: "candidate_launcher.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "candidate_install.json"; DestDir: "{app}"; Flags: ignoreversion
#endif
#ifdef PairedReleaseBuild
Source: "paired_install.json"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
#ifdef CandidateBuild
Name: "{autoprograms}\{#AppDisplayName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\candidate_launcher.ps1"" -DataDir ""{localappdata}\{#InstallSubdir}"""; IconFilename: "{app}\SmartReport.exe"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\candidate_launcher.ps1"" -DataDir ""{localappdata}\{#InstallSubdir}"""; IconFilename: "{app}\SmartReport.exe"; Tasks: desktopicon
#else
Name: "{autoprograms}\{#AppDisplayName}"; Filename: "{app}\SmartReport.exe"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\SmartReport.exe"; Tasks: desktopicon
#endif

[Run]
#ifndef CandidateBuild
#ifndef PairedReleaseBuild
Filename: "{app}\SmartReport.exe"; Description: "Запустить SmartReport"; Flags: nowait postinstall skipifsilent
#endif
#endif

; Данные пользователя (%LOCALAPPDATA%\SmartReport) при удалении программы сохраняются.

; Установщик SmartReport: без прав администратора, в профиль пользователя. Секретов не содержит.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\SmartReport"
#endif

[Setup]
AppId={{6C0B6F5E-5E57-4F0B-9B34-5A7D1C0A9E11}
AppName=SmartReport
AppVersion={#AppVersion}
AppPublisher=SmartReport
DefaultDirName={localappdata}\Programs\SmartReport
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
CloseApplications=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\SmartReport"; Filename: "{app}\SmartReport.exe"
Name: "{autodesktop}\SmartReport"; Filename: "{app}\SmartReport.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SmartReport.exe"; Description: "Запустить SmartReport"; Flags: nowait postinstall skipifsilent

; Данные пользователя (%LOCALAPPDATA%\SmartReport) при удалении программы сохраняются.

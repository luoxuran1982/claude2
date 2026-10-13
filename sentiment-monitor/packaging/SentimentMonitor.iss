; Windows 安装程序（Inno Setup 6）。CI 里：iscc /DAppVersion=0.2.0 packaging\SentimentMonitor.iss
; 安装到 %LOCALAPPDATA%\Programs\SentimentMonitor，不需要管理员权限；卸载时保留用户数据（%APPDATA%\SentimentMonitor）。
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1E2B8C-4C4B-4E36-9C1A-5D0B7A2F9E31}
AppName=舆情监控
AppVersion={#AppVersion}
AppPublisher=SentimentMonitor
DefaultDirName={localappdata}\Programs\SentimentMonitor
DefaultGroupName=舆情监控
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=SentimentMonitor-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=舆情监控 {#AppVersion}

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[Files]
Source: "..\dist\SentimentMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\舆情监控"; Filename: "{app}\SentimentMonitor.exe"
Name: "{group}\卸载舆情监控"; Filename: "{uninstallexe}"
Name: "{userdesktop}\舆情监控"; Filename: "{app}\SentimentMonitor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SentimentMonitor.exe"; Description: "立即打开舆情监控"; Flags: nowait postinstall skipifsilent

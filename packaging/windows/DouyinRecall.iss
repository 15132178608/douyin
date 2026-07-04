#define MyAppName "Douyin Recall"
#define MyAppVersion "0.1.12"
#define MyAppPublisher "xiaojiang"

#ifndef SourceRoot
#define SourceRoot "..\.."
#endif

[Setup]
AppId={{8D520E24-23C6-4C2E-8C2D-7AF8A935E32F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\DouyinRecall
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#SourceRoot}\packaging\windows\out
OutputBaseFilename=DouyinRecallSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayIcon={app}\packaging\windows\start-douyin-recall.ps1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs; Excludes: ".git\*,.venv\*,data\*,.env,.env.local,.claude\*,.pytest_cache\*,.ruff_cache\*,.mypy_cache\*,pytest-cache-files-*\*,*.pyc,AGENTS.md,packaging\windows\out\*"

[Icons]
Name: "{autoprograms}\Douyin Recall"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Control"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Status"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""status"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Stop Service"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""stop"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Maintenance"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""maintenance"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Diagnostics"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""diagnose"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Logs"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""logs"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Health Check"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""health"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Repair State"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""repair"""; WorkingDir: "{app}"
Name: "{autodesktop}\Douyin Recall"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; Description: "Launch Douyin Recall"; Flags: postinstall nowait skipifsilent

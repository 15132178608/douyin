#define MyAppName "Douyin Recall"
#define MyAppVersion "0.1.15"
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
Source: "{#SourceRoot}\packaging\windows\preinstall-backup-douyin-recall.ps1"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\Douyin Recall"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Control"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Status"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""status"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Stop Service"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""stop"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Maintenance"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""maintenance"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Account Recovery"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""auth"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Diagnostics"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""diagnose"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Logs"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""logs"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Health Check"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""health"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Repair State"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""repair"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Backup Now"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""backup"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Backups"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""backups"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Restore Center"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""restore"""; WorkingDir: "{app}"
Name: "{autoprograms}\Douyin Recall Verify Backup"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""verify-backup"""; WorkingDir: "{app}"
Name: "{autodesktop}\Douyin Recall"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\start-douyin-recall.ps1"""; Description: "Launch Douyin Recall"; Flags: postinstall nowait skipifsilent

[Code]
function PreInstallBackupTimestamp(): String;
begin
  Result := GetDateTimeString('yyyymmdd-hhnnss', '', '');
end;

procedure CreatePreInstallDatabaseBackup();
var
  SourceDb: String;
  BackupDir: String;
  BackupPath: String;
  ScriptPath: String;
  PowerShellArgs: String;
  ResultCode: Integer;
  ExecOk: Boolean;
begin
  SourceDb := ExpandConstant('{app}\data\recall.db');
  if not FileExists(SourceDb) then
  begin
    Log('Pre-install backup skipped: recall.db not found.');
    Exit;
  end;

  BackupDir := ExpandConstant('{app}\data\exports');
  if not ForceDirectories(BackupDir) then
  begin
    Log('Pre-install database backup failed: could not create ' + BackupDir);
    if not WizardSilent() then
    begin
      MsgBox('Could not create Douyin Recall backup directory: ' + BackupDir, mbError, MB_OK);
    end;
    Exit;
  end;

  ExtractTemporaryFile('preinstall-backup-douyin-recall.ps1');
  ScriptPath := ExpandConstant('{tmp}\preinstall-backup-douyin-recall.ps1');
  PowerShellArgs := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" -AppRoot "' + ExpandConstant('{app}') + '"';
  ExecOk := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), PowerShellArgs, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ExecOk and (ResultCode = 0) then
  begin
    Log('Pre-install database backup created by PowerShell helper.');
    Exit;
  end;

  Log('Pre-install database backup helper failed; falling back to direct file copy.');
  BackupPath := BackupDir + '\pre-install-recall-' + PreInstallBackupTimestamp() + '.db';
  if CopyFile(SourceDb, BackupPath, False) then
  begin
    Log('Pre-install database backup: ' + BackupPath);
  end
  else
  begin
    Log('Pre-install database backup failed: ' + BackupPath);
    if not WizardSilent() then
    begin
      MsgBox('Could not create Douyin Recall pre-install database backup. You can continue, but creating a manual backup first is recommended.', mbError, MB_OK);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    CreatePreInstallDatabaseBackup();
  end;
end;

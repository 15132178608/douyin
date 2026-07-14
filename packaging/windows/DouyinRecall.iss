#define MyAppName "Douyin Recall"
#define MyAppVersion "0.1.25"
#define MyAppPublisher "xiaojiang"

#if Ver < EncodeVer(6, 5, 0)
  #error Douyin Recall requires Inno Setup 6.5.0 or later
#endif

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
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#SourceRoot}\packaging\windows\out
OutputBaseFilename=DouyinRecallSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
SetupIconFile={#SourceRoot}\packaging\windows\DouyinRecall.ico
UninstallDisplayIcon={app}\packaging\windows\DouyinRecall.ico

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标"; Flags: unchecked
Name: "prepareruntime"; Description: "安装后下载并准备首次运行所需组件"; GroupDescription: "首次运行准备"; Check: ShouldOfferRuntimePreparationTask

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs; Excludes: ".git\*,.venv\*,data\*,dist\*,.env,.env.local,.claude\*,.pytest_cache\*,.ruff_cache\*,.mypy_cache\*,pytest-cache-files-*\*,*.pyc,AGENTS.md,packaging\windows\out\*"
Source: "{#SourceRoot}\packaging\windows\preinstall-backup-douyin-recall.ps1"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\Douyin Recall"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\packaging\windows\launch-douyin-recall.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Control"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Status"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""status"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Prepare Runtime"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""prepare"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Stop Service"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""stop"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Maintenance"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""maintenance"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Account Recovery"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""auth"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Diagnostics"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""diagnose"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Logs"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""logs"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Health Check"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""health"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Repair State"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""repair"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Backup Now"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""backup"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Backups"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""backups"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Restore Center"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""restore"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Verify Backup"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""verify-backup"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autoprograms}\Douyin Recall Rollback Check"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\control-douyin-recall.ps1"" -Action ""rollback-check"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"
Name: "{autodesktop}\Douyin Recall"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\packaging\windows\launch-douyin-recall.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\DouyinRecall.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\packaging\windows\launch-douyin-recall.vbs"""; Description: "安装完成后启动 Douyin Recall"; Flags: postinstall unchecked nowait runhidden skipifsilent; Check: ShouldLaunchAfterInstall

[Code]
var
  RuntimePreparationPage: TOutputProgressWizardPage;
  RuntimePreparationIsUpgrade: Boolean;
  RuntimePreparationDeferred: Boolean;
  RuntimePreparationProtocolFailed: Boolean;
  RuntimePreparationProtocolComplete: Boolean;
  RuntimePreparationCurrent: Integer;
  RuntimePreparationTotal: Integer;

function ExistingInstallationDetected(): Boolean;
var
  UninstallKey: String;
begin
  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D520E24-23C6-4C2E-8C2D-7AF8A935E32F}_is1';
  Result := RegKeyExists(HKCU, UninstallKey) or RegKeyExists(HKLM, UninstallKey);
end;

procedure InitializeWizard();
begin
  RuntimePreparationIsUpgrade := ExistingInstallationDetected();
  RuntimePreparationDeferred := False;
  RuntimePreparationPage := CreateOutputProgressPage(
    '准备 Douyin Recall 运行环境',
    '正在下载并初始化运行依赖，请稍候。');
end;

function ShouldOfferRuntimePreparationTask(): Boolean;
begin
  Result := not RuntimePreparationIsUpgrade;
end;

function NextProgressField(var Remaining: String): String;
var
  SeparatorPos: Integer;
begin
  SeparatorPos := Pos('|', Remaining);
  if SeparatorPos = 0 then
  begin
    Result := Remaining;
    Remaining := '';
  end
  else
  begin
    Result := Copy(Remaining, 1, SeparatorPos - 1);
    Delete(Remaining, 1, SeparatorPos);
  end;
end;

function ProgressNumber(const Value: String; const Default: Integer): Integer;
begin
  try
    Result := StrToInt(Value);
  except
    Result := Default;
  end;
end;

function ClampProgress(const Value, Maximum: Integer): Integer;
begin
  Result := Value;
  if Result < 0 then
    Result := 0;
  if (Maximum > 0) and (Result > Maximum) then
    Result := Maximum;
end;

function LatestActivityText(const Value: String): String;
begin
  Result := Trim(Value);
  if Length(Result) > 240 then
    Result := Copy(Result, 1, 237) + '...';
end;

procedure RuntimePreparationOutput(const S: String; const Error, FirstLine: Boolean);
var
  Remaining: String;
  EventName: String;
  IndexText: String;
  TotalText: String;
  StepKey: String;
  StepLabel: String;
  StatusText: String;
  StepIndex: Integer;
  StepTotal: Integer;
begin
  Log('Runtime preparation output: ' + S);

  if Pos('DR_PROGRESS|', S) <> 1 then
  begin
    StatusText := LatestActivityText(S);
    if StatusText <> '' then
    begin
      if Error then
        StatusText := '工具输出：' + StatusText
      else
        StatusText := '最新活动：' + StatusText;
      RuntimePreparationPage.SetText('正在准备运行环境…', StatusText);
    end;
    Exit;
  end;

  Remaining := Copy(S, Length('DR_PROGRESS|') + 1, MaxInt);
  EventName := Uppercase(NextProgressField(Remaining));
  IndexText := NextProgressField(Remaining);
  TotalText := NextProgressField(Remaining);
  StepKey := NextProgressField(Remaining);
  StepLabel := Remaining;

  StepIndex := ProgressNumber(IndexText, RuntimePreparationCurrent);
  StepTotal := ProgressNumber(TotalText, RuntimePreparationTotal);
  if StepTotal > 0 then
    RuntimePreparationTotal := StepTotal;
  if StepIndex >= 0 then
    RuntimePreparationCurrent := StepIndex;
  if StepLabel = '' then
    StepLabel := StepKey;

  if EventName = 'BEGIN' then
  begin
    RuntimePreparationPage.SetProgress(
      ClampProgress(RuntimePreparationCurrent - 1, RuntimePreparationTotal),
      RuntimePreparationTotal);
    StatusText := Format('[%d/%d] %s', [RuntimePreparationCurrent, RuntimePreparationTotal, StepLabel]);
  end
  else if EventName = 'DONE' then
  begin
    RuntimePreparationPage.SetProgress(
      ClampProgress(RuntimePreparationCurrent, RuntimePreparationTotal),
      RuntimePreparationTotal);
    StatusText := Format('[%d/%d] 已完成：%s', [RuntimePreparationCurrent, RuntimePreparationTotal, StepLabel]);
  end
  else if EventName = 'SKIP' then
  begin
    RuntimePreparationPage.SetProgress(
      ClampProgress(RuntimePreparationCurrent, RuntimePreparationTotal),
      RuntimePreparationTotal);
    StatusText := Format('[%d/%d] 已跳过：%s', [RuntimePreparationCurrent, RuntimePreparationTotal, StepLabel]);
  end
  else if EventName = 'FAILED' then
  begin
    RuntimePreparationProtocolFailed := True;
    RuntimePreparationPage.SetProgress(
      ClampProgress(RuntimePreparationCurrent - 1, RuntimePreparationTotal),
      RuntimePreparationTotal);
    StatusText := Format('[%d/%d] 失败：%s', [RuntimePreparationCurrent, RuntimePreparationTotal, StepLabel]);
  end
  else if EventName = 'BUSY' then
  begin
    RuntimePreparationProtocolFailed := True;
    StatusText := '已有运行环境准备任务正在进行；请等待它完成后再重试。';
  end
  else if EventName = 'COMPLETE' then
  begin
    RuntimePreparationProtocolComplete := True;
    if RuntimePreparationTotal > 0 then
      RuntimePreparationPage.SetProgress(RuntimePreparationTotal, RuntimePreparationTotal);
    StatusText := '运行环境准备完成。';
  end
  else
  begin
    StatusText := LatestActivityText(S);
  end;

  RuntimePreparationPage.SetText('正在准备运行环境…', StatusText);
end;

function ExecuteRuntimePreparation(var ResultCode: Integer): Boolean;
var
  PowerShellArgs: String;
  ExecOk: Boolean;
begin
  RuntimePreparationProtocolFailed := False;
  RuntimePreparationProtocolComplete := False;
  RuntimePreparationCurrent := 0;
  RuntimePreparationTotal := 0;
  RuntimePreparationPage.SetText(
    '正在准备运行环境…',
    '即将检查并下载所需组件。');
  RuntimePreparationPage.SetProgress(0, 0);

  PowerShellArgs :=
    '-NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\packaging\windows\control-douyin-recall.ps1') +
    '" -Action "prepare" -NonInteractive';
  try
    ExecOk := ExecAndLogOutput(
      ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      PowerShellArgs,
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode,
      @RuntimePreparationOutput);
  except
    ResultCode := -1;
    ExecOk := False;
    Log('Runtime preparation execution error: ' + GetExceptionMessage);
  end;
  Result := ExecOk and (ResultCode = 0) and
    RuntimePreparationProtocolComplete and
    (not RuntimePreparationProtocolFailed);
end;

procedure PrepareRuntimeAfterInstall();
var
  ResultCode: Integer;
  Choice: Integer;
  PreparationSucceeded: Boolean;
begin
  RuntimePreparationPage.Show;
  try
    repeat
      PreparationSucceeded := ExecuteRuntimePreparation(ResultCode);
      if PreparationSucceeded then
      begin
        RuntimePreparationDeferred := False;
        Log('Runtime preparation completed successfully.');
        Exit;
      end;

      Log(Format(
        'Runtime preparation failed (exit code %d, protocol complete=%d, protocol failed=%d).', [ResultCode, Ord(RuntimePreparationProtocolComplete), Ord(RuntimePreparationProtocolFailed)]));
      RuntimePreparationPage.SetText(
        '运行环境尚未准备完成',
        '可以立即重试，或稍后从开始菜单的“Douyin Recall Prepare Runtime”继续。');
      Choice := MsgBox(
        '准备 Douyin Recall 运行环境失败。'#13#10#13#10 +
        '选择“重试”会立即再试一次。'#13#10 +
        '选择“取消”会稍后处理：安装仍会完成，但本次不会紧接着启动应用。',
        mbError,
        MB_RETRYCANCEL);
      if Choice <> IDRETRY then
      begin
        RuntimePreparationDeferred := True;
        Log('Runtime preparation deferred by the user.');
        Exit;
      end;
    until False;
  finally
    RuntimePreparationPage.Hide;
  end;
end;

function ShouldLaunchAfterInstall(): Boolean;
begin
  Result := not RuntimePreparationDeferred;
end;

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

  if (CurStep = ssPostInstall) and
    (not WizardSilent()) and
    (not RuntimePreparationIsUpgrade) then
  begin
    if WizardIsTaskSelected('prepareruntime') then
      PrepareRuntimeAfterInstall()
    else
    begin
      RuntimePreparationDeferred := True;
      Log('Runtime preparation deferred because the prepareruntime task was not selected.');
    end;
  end;
end;

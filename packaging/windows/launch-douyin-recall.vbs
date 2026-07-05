Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appRoot = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
scriptPath = fso.BuildPath(scriptDir, "start-douyin-recall.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"

shell.CurrentDirectory = appRoot
cmd = Quote(powershellPath) & " -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File " & Quote(scriptPath) & " -Silent"
shell.Run cmd, 0, False

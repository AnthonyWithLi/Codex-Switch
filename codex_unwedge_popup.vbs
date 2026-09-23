Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = dir & "\start_codex_desktop.ps1"
cmd = "powershell.exe -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & ps1 & """"
Set sh = CreateObject("Wscript.Shell")
sh.Run cmd, 0, True

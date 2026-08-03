Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\.venv\Scripts\python.exe"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
sh.Run """" & py & """ -m betbot.launcher --stop", 0, True
MsgBox "BetBot se ha cerrado.", 64, "BetBot"

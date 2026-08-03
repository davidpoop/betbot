' Abre BetBot sin mostrar consola. Si ya esta abierto, reenfoca la pestana.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
  MsgBox "BetBot no esta instalado todavia." & vbCrLf & _
         "Ejecuta primero INSTALAR_BETBOT.bat (doble clic).", 48, "BetBot"
  WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
sh.Run """" & pyw & """ -m betbot.launcher", 0, False

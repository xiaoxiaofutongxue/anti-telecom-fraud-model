Option Explicit
Dim fs, shell, root, interpreter, entry
Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fs.GetParentFolderName(WScript.ScriptFullName)
interpreter = fs.BuildPath(root, ".venv\Scripts\pythonw.exe")
entry = fs.BuildPath(root, "app.py")
If Not fs.FileExists(interpreter) Then
    MsgBox "Python environment missing. See README.md.", 16, "FourFlowAnalysis"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & interpreter & Chr(34) & " " & Chr(34) & entry & Chr(34), 1, False

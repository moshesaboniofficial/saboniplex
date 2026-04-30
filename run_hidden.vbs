Option Explicit

Dim shell, fso, scriptDir, pythonw, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

pythonw = """" & scriptDir & "\.venv\Scripts\pythonw.exe" & """"
command = pythonw & " " & """" & scriptDir & "\saboniplex_gui.py" & """"

shell.Run command, 0, False

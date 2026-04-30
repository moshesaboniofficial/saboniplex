@echo off

rem NOTE: A .bat file always spawns a console briefly when double-clicked.
rem This launches the app via a windowless VBS wrapper and exits immediately.
wscript.exe "%~dp0run_hidden.vbs"
exit /b
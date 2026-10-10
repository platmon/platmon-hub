@echo off
powershell.exe -NoProfile -File "%~dp0deploy\start-windows.ps1" %*
exit /b %errorlevel%

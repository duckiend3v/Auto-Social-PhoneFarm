@echo off
setlocal
cd /d %~dp0
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1"
endlocal

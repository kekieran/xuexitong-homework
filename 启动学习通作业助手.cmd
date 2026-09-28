@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\portable.ps1" -Mode start
if errorlevel 1 (
  echo Startup failed. Please read the message above.
  pause
  exit /b 1
)

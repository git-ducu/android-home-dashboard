@echo off
fltmc >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WindowStyle Hidden"
  exit /b
)
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0dashboard_task.ps1" uninstall

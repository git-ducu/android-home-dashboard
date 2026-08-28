@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI\"
cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%runtime\platform-tools\adb.exe" (
  echo [ERROR] Local runtime is missing. Run install.bat first.
  pause
  exit /b 1
)
fltmc >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard_task.ps1" restart
if errorlevel 1 exit /b 1
endlocal

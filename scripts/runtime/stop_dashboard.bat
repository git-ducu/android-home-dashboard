@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI\"
cd /d "%PROJECT_ROOT%"
fltmc >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%~f0' -Verb RunAs -WindowStyle Hidden -PassThru; $p.WaitForExit(); exit $p.ExitCode"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard_task.ps1" stop
exit /b %errorlevel%

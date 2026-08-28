@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI\"
cd /d "%PROJECT_ROOT%"

if not exist "%PROJECT_ROOT%runtime\platform-tools\adb.exe" (
  echo [ERROR] Local ADB runtime is missing. Run install.bat first.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0wireless_setup.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Wireless setup failed with exit code %RESULT%.
pause
exit /b %RESULT%

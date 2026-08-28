@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI\"
cd /d "%PROJECT_ROOT%"
title Android Home Dashboard Installer
set "QUIET="
if /i "%~1"=="/quiet" set "QUIET=1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

rem Opening a BAT directly from a ZIP, browser, or mail attachment can copy
rem only this file to a cache directory. Validate the complete payload before
rem requesting elevation or changing the machine.
set "PAYLOAD_MISSING="
for %%F in (
  "scripts\install\install_runtime.ps1"
  "scripts\runtime\dashboard_task.ps1"
  "scripts\runtime\phone_shutdown.ps1"
  "src\bridge\pc_bridge.py"
  "src\web\dashboard.html"
  "src\web\manifest.webmanifest"
  "src\web\sw.js"
  "src\web\assets\icon.svg"
  "src\web\assets\icon-192.png"
  "src\web\assets\icon-512.png"
) do (
  if not exist "%PROJECT_ROOT%%%~F" (
    echo [ERROR] Required file is missing: %%~F
    set "PAYLOAD_MISSING=1"
  )
)
if defined PAYLOAD_MISSING goto :incomplete_package

rem Scheduled tasks keep absolute paths, so an installation rooted in a
rem browser cache or TEMP would break as soon as Windows cleans that folder.
set "DASHBOARD_INSTALL_DIR=%PROJECT_ROOT%"
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $root=[IO.Path]::GetFullPath($env:DASHBOARD_INSTALL_DIR); $roots=@($env:TEMP,$env:TMP); if($env:LOCALAPPDATA){$roots+=(Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\INetCache')}; foreach($item in $roots){if([string]::IsNullOrWhiteSpace($item)){continue}; $prefix=[IO.Path]::GetFullPath($item).TrimEnd('\')+'\'; if($root.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){exit 10}}; exit 0 } catch { exit 11 }"
set "LOCATION_EXIT=%ERRORLEVEL%"
if "%LOCATION_EXIT%"=="10" goto :temporary_location
if not "%LOCATION_EXIT%"=="0" goto :location_check_failed

"%SystemRoot%\System32\fltmc.exe" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :run_install

rem Pass the path through the environment instead of embedding it in a quoted
rem PowerShell program. This also works when a folder name contains an apostrophe.
set "DASHBOARD_INSTALLER=%~f0"
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $p=Start-Process -FilePath $env:DASHBOARD_INSTALLER -ArgumentList '/quiet' -Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch { Write-Error $_; exit 1 }"
set "ELEVATION_EXIT=%ERRORLEVEL%"
if not "%ELEVATION_EXIT%"=="0" goto :elevation_failed
goto :success

:run_install
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_runtime.ps1"
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" goto :install_failed
goto :success

:incomplete_package
echo.
echo [ERROR] The installer package is incomplete.
echo Do not open install.bat inside a ZIP file or from a browser/download preview.
echo Extract the entire package to a permanent folder, then run install.bat again.
if not defined QUIET pause
exit /b 2

:temporary_location
echo.
echo [ERROR] The installer is running from a temporary browser or ZIP folder.
echo Extract the entire package to a permanent folder, then run install.bat again.
if not defined QUIET pause
exit /b 2

:location_check_failed
echo.
echo [ERROR] The installer location could not be validated.
if not defined QUIET pause
exit /b 1

:elevation_failed
echo.
echo [ERROR] Administrator installation failed or the UAC prompt was cancelled.
if not defined QUIET pause
exit /b 1

:install_failed
echo.
echo [ERROR] Installation failed. PowerShell exit code: %INSTALL_EXIT%
if not defined QUIET pause
exit /b 1

:success
echo.
echo [SUCCESS] Local installation complete.
if not defined QUIET pause
exit /b 0

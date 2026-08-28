@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  Android Home Dashboard - single control script
rem  Usage: dashboard.bat [start | stop | restart | status | install | uninstall]
rem    start / (no arg) : clean start or refresh the bridge and phone dashboard
rem    stop             : fully shut down bridge, PresentMon, ADB and phone
rem    restart          : stop then start again
rem    status           : show autostart / process / health state
rem    install          : (re)register the autostart + shutdown scheduled tasks
rem    uninstall        : stop everything and remove the scheduled tasks
rem ---------------------------------------------------------------------------

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

rem Friendly aliases
if /I "%ACTION%"=="on"      set "ACTION=start"
if /I "%ACTION%"=="off"     set "ACTION=stop"
if /I "%ACTION%"=="reload"  set "ACTION=restart"
if /I "%ACTION%"=="reboot"  set "ACTION=restart"

if /I not "%ACTION%"=="start" if /I not "%ACTION%"=="stop" if /I not "%ACTION%"=="restart" if /I not "%ACTION%"=="status" if /I not "%ACTION%"=="install" if /I not "%ACTION%"=="uninstall" (
  echo Usage: dashboard.bat [start ^| stop ^| restart ^| status ^| install ^| uninstall]
  exit /b 1
)

if not exist "%~dp0runtime\platform-tools\adb.exe" (
  echo [ERROR] Local runtime is missing. Run install.bat first.
  pause
  exit /b 1
)

rem Elevate once; every action needs admin (scheduled tasks, ETW, taskkill).
fltmc >nul 2>&1
if not errorlevel 1 goto :already_admin
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%~f0' -ArgumentList '%ACTION%' -Verb RunAs -PassThru; $p.WaitForExit(); exit $p.ExitCode"
exit /b %errorlevel%

:already_admin
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard_task.ps1" %ACTION%
exit /b %errorlevel%

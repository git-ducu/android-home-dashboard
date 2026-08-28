@echo off
setlocal
cd /d "%~dp0"
title A51 Dashboard v2.0 Diagnose
set "PYTHON=%~dp0runtime\python\python.exe"
set "ADB=%~dp0runtime\platform-tools\adb.exe"
set "PRESENTMON=%~dp0runtime\presentmon\PresentMon.exe"
set "ADB_VENDOR_KEYS=%~dp0runtime\adb-keys\adbkey"
set "ANDROID_USER_HOME=%~dp0runtime\android-home"

echo ===== Python =====
"%PYTHON%" --version

echo.
echo ===== Packages =====
"%PYTHON%" -c "import psutil; print('psutil', psutil.__version__)"
"%PYTHON%" -c "from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager; from winrt.windows.storage.streams import Buffer; print('WinRT Media: OK')" 2>&1

echo.
echo ===== ADB =====
"%ADB%" version
"%ADB%" devices -l
"%ADB%" mdns check
"%ADB%" mdns services
for /f "tokens=1,2" %%A in ('"%ADB%" devices') do if "%%B"=="device" (
  echo Reverse mappings for %%A
  "%ADB%" -s %%A reverse --list
)

echo.
echo ===== NVIDIA =====
where nvidia-smi.exe
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo.
echo ===== PresentMon =====
"%PRESENTMON%" --help >nul 2>&1 && echo PresentMon: OK

echo.
echo ===== Physical disks =====
powershell -NoProfile -Command "Get-PhysicalDisk ^| Select FriendlyName,MediaType,BusType,HealthStatus,Size ^| Format-Table -AutoSize"

echo.
echo ===== Bridge =====
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri http://127.0.0.1:8765/api/status -TimeoutSec 4; Write-Host ('Version: ' + $r.version); Write-Host ('CPU: ' + $r.cpu.percent + ' source=' + $r.cpu.source); Write-Host ('Media available: ' + $r.media.available); Write-Host ('Media error: ' + $r.media.error); Write-Host ('Foreground: ' + $r.foreground.process) } catch { Write-Host $_.Exception.Message }"

echo.
pause
endlocal

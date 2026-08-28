param([ValidateSet('install','run','start','stop','restart','status','uninstall')][string]$Action = 'restart')
$ErrorActionPreference = 'Stop'
$taskName = 'Android Home Dashboard'
$shutdownTaskName = 'Android Home Dashboard Shutdown'
$legacyTaskNames = @('A51 PC Dashboard')
$legacyShutdownTaskNames = @('A51 PC Dashboard Shutdown')
$taskScript = $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root 'pc_bridge.py'
$phoneShutdownScript = Join-Path $root 'phone_shutdown.ps1'
$pidFile = Join-Path $root 'bridge.pid'
$launcherLog = Join-Path $root 'logs\launcher.log'
$port = 8765
$bridgeVersion = '2.4'
$healthUri = "http://127.0.0.1:$port/api/health"
$securityUri = "http://127.0.0.1:$port/api/security"
$shutdownUri = "http://127.0.0.1:$port/api/shutdown"
$pythonNames = @('python.exe', 'pythonw.exe', 'py.exe')

function Write-LauncherLog([string]$Message) {
    try {
        $logDirectory = Split-Path -Parent $launcherLog
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (
            '[{0}] {1}' -f (Get-Date).ToString('s'), $Message
        )
    } catch {}
}

# Authoritative process lookup: only python processes actually running
# pc_bridge.py. Matching on the command line instead of a cached PID avoids
# killing an unrelated process that Windows recycled the old PID onto.
function Get-BridgeProcess {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in $pythonNames -and $_.CommandLine -like '*pc_bridge.py*'
    }
}

function Stop-BridgeProcess {
    foreach ($proc in Get-BridgeProcess) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-PortOwner {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $listener.OwningProcess) -ErrorAction SilentlyContinue
        if ($proc -and $proc.Name -in $pythonNames) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-PresentMon {
    Get-Process presentmon -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 150
    # Orphaned ETW sessions block the next PresentMon start; remove current and legacy sessions.
    $sessions = logman query -ets 2>$null
    foreach ($line in $sessions) {
        if ($line -match '^\s*((?:AndroidHomeDashboard|A51Dashboard)\S*)\s+') {
            logman stop $Matches[1] -ets 2>$null | Out-Null
        }
    }
}

function Stop-Adb {
    # adb starts a detached server, so killing the client that launched it can
    # leave an orphaned adb.exe behind. Kill the whole tree and confirm it is gone.
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        & taskkill.exe /F /T /IM adb.exe 2>&1 | Out-Null
        $ErrorActionPreference = $saved
        Get-Process adb -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 200
        if (-not (Get-Process adb -ErrorAction SilentlyContinue)) { break }
    }
}

function Wait-BridgeGone([int]$TimeoutMs = 5000) {
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    while ((Get-Date) -lt $deadline) {
        $alive = Get-BridgeProcess
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if (-not $alive -and -not $listening) { return $true }
        Start-Sleep -Milliseconds 150
    }
    return $false
}

function Wait-BridgeReady([int]$TimeoutMs = 20000) {
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 2
            if ($health.version -eq $bridgeVersion) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    return $false
}

# Force a clean slate before every start and stop. A crash or an unclean Windows
# shutdown can leave a zombie bridge, an occupied port, orphaned PresentMon ETW
# sessions, or a stale PID file behind.
function Clear-BridgeRuntime {
    Stop-BridgeProcess
    Stop-PortOwner
    Stop-PresentMon
    Wait-BridgeGone 4000 | Out-Null
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $env:TEMP 'a51-presentmon-*.csv') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $root '__pycache__') -Recurse -Force -ErrorAction SilentlyContinue
}

function Invoke-BridgeRun {
    $pythonw = Join-Path $root 'runtime\python\pythonw.exe'
    $adb = Join-Path $root 'runtime\platform-tools\adb.exe'
    if (-not (Test-Path $pythonw)) { throw 'Local Python runtime is missing. Run install.bat.' }
    if (-not (Test-Path $adb)) { throw 'Local ADB runtime is missing. Run install.bat.' }
    $env:ADB_VENDOR_KEYS = Join-Path $root 'runtime\adb-keys\adbkey'
    $env:ANDROID_USER_HOME = Join-Path $root 'runtime\android-home'

    # Every launch (manual or at logon) begins from a guaranteed-clean state.
    Clear-BridgeRuntime
    Write-LauncherLog 'Starting bridge; phone discovery will retry ADB independently.'
    $bridge = Start-Process -FilePath $pythonw -ArgumentList @('-B', ('"{0}"' -f $script)) -WorkingDirectory $root -WindowStyle Hidden -PassThru
    if (-not $bridge) { throw 'Bridge process failed to launch.' }
    if (-not (Wait-BridgeReady 20000)) {
        if (-not $bridge.HasExited) {
            Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue
        }
        throw "Android Home Dashboard bridge v$bridgeVersion did not become ready."
    }
    Write-LauncherLog ("Bridge v{0} ready (PID {1})." -f $bridgeVersion, $bridge.Id)
    $bridge.WaitForExit()
    Write-LauncherLog ("Bridge exited with code {0}." -f $bridge.ExitCode)
    exit $bridge.ExitCode
}

function Start-Dashboard {
    $registeredTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $registeredTask) {
        $registeredTask = $legacyTaskNames | ForEach-Object {
            Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
        } | Select-Object -First 1
    }
    if (-not $registeredTask) {
        throw 'Autostart task is missing. Run install.bat (or enable_autostart.bat) first.'
    }
    Stop-Dashboard $false
    Start-ScheduledTask -TaskName $registeredTask.TaskName
    if (-not (Wait-BridgeReady 20000)) {
        throw "Android Home Dashboard bridge v$bridgeVersion did not become ready."
    }
}

function Get-DashboardStatus {
    $running = [bool](Get-BridgeProcess)
    $ready = $false
    try { $ready = ((Invoke-RestMethod -Uri $healthUri -TimeoutSec 2).version -eq $bridgeVersion) } catch {}
    # An elevated scheduled-task process can hide its command line from a
    # non-elevated status shell. A matching local health response is stronger
    # evidence that the bridge process is running.
    if ($ready) { $running = $true }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        $task = $legacyTaskNames | ForEach-Object {
            Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
        } | Select-Object -First 1
    }
    [PSCustomObject]@{
        Autostart = if ($task) { 'registered' } else { 'not registered' }
        Process   = if ($running) { 'running' } else { 'stopped' }
        Health    = if ($ready) { 'ready' } else { 'unavailable' }
    } | Format-List
}

function Install-DashboardTask {
    $pythonw = Join-Path $root 'runtime\python\pythonw.exe'
    if (-not (Test-Path $pythonw)) { throw 'Local Python runtime is missing. Run install.bat.' }
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" run' -f $taskScript) -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

    $shutdownAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" stop' -f $taskScript) -WorkingDirectory $root
    $eventClass = Get-CimClass -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClassName 'MSFT_TaskEventTrigger'
    $shutdownTrigger = New-CimInstance -CimClass $eventClass -ClientOnly
    $shutdownTrigger.Enabled = $true
    $shutdownTrigger.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''User32''] and EventID=1074]]</Select></Query></QueryList>'
    Register-ScheduledTask -TaskName $shutdownTaskName -Action $shutdownAction -Trigger $shutdownTrigger -Principal $principal -Settings $settings -Force | Out-Null
    $legacyTaskNames | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
    }
    $legacyShutdownTaskNames | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
    }
}

function Stop-Dashboard([bool]$KillAdb = $true) {
    # Gracefully close the phone screen guardian on both stop and restart. A
    # forced bridge kill can strand an adb shell client while its Wi-Fi stream
    # is closing, which delays the next launch.
    try {
        $security = Invoke-RestMethod -Uri $securityUri -TimeoutSec 2
        $headers = @{ 'X-A51-Token' = [string]$security.token }
        $shutdownResult = Invoke-RestMethod -Method Post -Uri $shutdownUri -Headers $headers -TimeoutSec 12
        if ($shutdownResult.phone_shutdown_complete) { Start-Sleep -Milliseconds 200 }
    } catch {}
    # Stop the autostart task first so it cannot relaunch the bridge mid-cleanup.
    @($taskName) + $legacyTaskNames | ForEach-Object {
        Stop-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 200
    Clear-BridgeRuntime
    if ($KillAdb) {
        # Idempotent safety net: force-stop the cached PWA even if the API call
        # above returned early, then stop the ADB server tree.
        & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $phoneShutdownScript
        Stop-Adb
    }
    Get-ChildItem -LiteralPath (Join-Path $root 'logs') -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

try {
    switch ($Action) {
        install { Install-DashboardTask }
        run { Invoke-BridgeRun }
        start { Start-Dashboard }
        stop { Stop-Dashboard }
        restart { Start-Dashboard }
        status { Get-DashboardStatus }
        uninstall {
            Stop-Dashboard
            @($taskName, $shutdownTaskName) + $legacyTaskNames + $legacyShutdownTaskNames | ForEach-Object {
                Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
            }
        }
    }
} catch {
    Write-LauncherLog ("Action '{0}' failed: {1}" -f $Action, $_.Exception.Message)
    Write-Error $_ -ErrorAction Continue
    exit 1
}

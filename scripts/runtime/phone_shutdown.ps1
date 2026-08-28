$ErrorActionPreference = 'SilentlyContinue'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$adb = Join-Path $root 'runtime\platform-tools\adb.exe'
$env:ADB_VENDOR_KEYS = Join-Path $root 'runtime\adb-keys\adbkey'
$env:ANDROID_USER_HOME = Join-Path $root 'runtime\android-home'
if (-not $adb) { exit 0 }
if (-not (Test-Path $adb)) { exit 0 }

function Invoke-AdbBounded([string[]]$Arguments, [int]$TimeoutMs = 4000) {
    $process = Start-Process -FilePath $adb -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutMs)) {
        $process.Kill()
        $process.WaitForExit()
        return $false
    }
    return $process.ExitCode -eq 0
}

# Recover wireless transports when the bridge is already unavailable. Modern
# Android devices advertise a dynamic TLS endpoint through mDNS; Android 10
# and lower use the saved legacy endpoint from wireless_setup.bat.
$configFile = Join-Path $root 'phone_config.json'
$config = $null
if (Test-Path -LiteralPath $configFile) {
    try { $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
}
if (-not $config -or $config.mode -ne 'usb') {
    $lastEndpointFile = Join-Path $root 'runtime\last-wireless-endpoint.txt'
    if (Test-Path -LiteralPath $lastEndpointFile) {
        $lastEndpoint = (Get-Content -LiteralPath $lastEndpointFile -Raw).Trim()
        if ($lastEndpoint -match '^\S+:[0-9]{1,5}$') {
            Invoke-AdbBounded @('connect', $lastEndpoint) 2500 | Out-Null
        }
    }
    foreach ($endpoint in @($config.legacy_endpoints)) {
        if ($endpoint -is [string] -and $endpoint -match '^\S+:[0-9]{1,5}$') {
            Invoke-AdbBounded @('connect', $endpoint) 2500 | Out-Null
        }
    }
    $mdns = & $adb mdns services 2>$null
    foreach ($line in $mdns) {
        if ($line -match '_adb-tls-connect\._tcp\s+(\S+:[0-9]+)\s*$') {
            Invoke-AdbBounded @('connect', $Matches[1]) 2500 | Out-Null
        }
    }
}

$serials = @()
$deviceLines = & $adb devices 2>$null
foreach ($line in $deviceLines) {
    if ($line -match '^(\S+)\s+device\s*$') { $serials += $Matches[1] }
}
if (-not $serials.Count) { exit 0 }

$targetSerial = $null
$preferredHardware = if ($config) { [string]$config.preferred_hardware_serial } else { '' }
if ($preferredHardware) {
    foreach ($serial in $serials) {
        if ($serial -eq $preferredHardware) {
            $targetSerial = $serial
            break
        }
        if ($serial.Contains(':')) {
            $identity = ((& $adb -s $serial shell getprop ro.serialno 2>$null) -join "`n").Trim()
            if ($identity -eq $preferredHardware) {
                $targetSerial = $serial
                break
            }
        }
    }
}
if (-not $targetSerial) {
    $targetSerial = @($serials | Where-Object { $_.Contains(':') } | Select-Object -First 1)[0]
}
if (-not $targetSerial) { $targetSerial = $serials[0] }

# One adb shell round-trip: `svc power stayon false` alone can take over a
# second, so separate 1s-bounded calls timed out before the screen ever slept.
# Clear stay-awake before SLEEP so the display stays off while USB is attached.
$steps = @()
$pwaPackageFile = Join-Path $root 'runtime\pwa-package.txt'
if (Test-Path $pwaPackageFile) {
    $pwaPackage = (Get-Content -LiteralPath $pwaPackageFile -Raw).Trim()
    if ($pwaPackage -match '^[A-Za-z0-9._]+$') {
        $steps += "am force-stop $pwaPackage"
    }
}
$steps += 'am force-stop com.android.chrome'
$steps += 'svc power stayon false'
$steps += 'input keyevent KEYCODE_SLEEP'
Invoke-AdbBounded @('-s', $targetSerial, 'shell', ($steps -join '; ')) 4000 | Out-Null

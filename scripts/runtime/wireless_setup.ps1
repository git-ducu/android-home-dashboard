param(
    [ValidateSet('setup','status','auto','usb')]
    [string]$Action = 'setup'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$adb = Join-Path $root 'runtime\platform-tools\adb.exe'
$configFile = Join-Path $root 'phone_config.json'
$env:ADB_VENDOR_KEYS = Join-Path $root 'runtime\adb-keys\adbkey'
$env:ANDROID_USER_HOME = Join-Path $root 'runtime\android-home'

if (-not (Test-Path -LiteralPath $adb)) {
    throw 'Local ADB runtime is missing. Run install.bat first.'
}

function Invoke-Adb([string[]]$Arguments, [switch]$AllowFailure) {
    $output = & $adb @Arguments 2>&1
    $code = $LASTEXITCODE
    if (-not $AllowFailure -and $code -ne 0) {
        throw "adb $($Arguments -join ' ') failed: $($output -join ' ')"
    }
    return $output
}

function Get-ConnectedDevices {
    $lines = Invoke-Adb @('devices','-l')
    $devices = @()
    foreach ($line in $lines | Select-Object -Skip 1) {
        if ($line -match '^([^\s]+)\s+device(?:\s|$)') {
            $serial = $Matches[1]
            $devices += [PSCustomObject]@{
                Serial = $serial
                Connection = if ($serial.Contains(':') -and -not $serial.StartsWith('emulator-')) { 'wireless' } else { 'usb' }
                Description = $line.Trim()
            }
        }
    }
    return $devices
}

function Get-DeviceValue([string]$Serial, [string[]]$Arguments) {
    $value = Invoke-Adb (@('-s',$Serial) + $Arguments)
    return (($value -join "`n").Trim())
}

function Read-Config {
    if (Test-Path -LiteralPath $configFile) {
        try { return Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
    }
    return [PSCustomObject]@{
        mode = 'auto'
        prefer_wireless = $true
        usb_fallback = $true
        preferred_hardware_serial = ''
        preferred_model = ''
        legacy_endpoints = @()
    }
}

function Write-Config($Config) {
    $json = $Config | ConvertTo-Json -Depth 4
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($configFile, $json + [Environment]::NewLine, $utf8NoBom)
}

function Show-Status {
    Write-Host '=== Saved phone configuration ==='
    if (Test-Path -LiteralPath $configFile) {
        Get-Content -LiteralPath $configFile -Encoding UTF8
    } else {
        Write-Host '(default: prefer wireless, fall back to USB)'
    }
    Write-Host ''
    Write-Host '=== Connected ADB transports ==='
    Invoke-Adb @('devices','-l') | ForEach-Object { Write-Host $_ }
    Write-Host ''
    Write-Host '=== Discovered wireless services ==='
    Invoke-Adb @('mdns','services') -AllowFailure | ForEach-Object { Write-Host $_ }
}

Invoke-Adb @('start-server') | Out-Null

if ($Action -eq 'status') {
    Show-Status
    exit 0
}

if ($Action -in @('auto','usb')) {
    $config = Read-Config
    $config.mode = if ($Action -eq 'usb') { 'usb' } else { 'auto' }
    $config.prefer_wireless = $Action -eq 'auto'
    $config.usb_fallback = $true
    Write-Config $config
    Write-Host "Connection mode changed to $($config.mode)."
    Write-Host 'The running dashboard will pick up this change automatically.'
    Show-Status
    exit 0
}

$usbDevices = @(Get-ConnectedDevices | Where-Object Connection -eq 'usb')
if ($usbDevices.Count -eq 0) {
    throw 'Connect the target phone by USB for identification, then run this setup again.'
}
if ($usbDevices.Count -gt 1) {
    Write-Host 'More than one USB phone is connected:'
    for ($i = 0; $i -lt $usbDevices.Count; $i++) {
        Write-Host "[$($i + 1)] $($usbDevices[$i].Description)"
    }
    $choice = [int](Read-Host 'Select the target number')
    if ($choice -lt 1 -or $choice -gt $usbDevices.Count) { throw 'Invalid selection.' }
    $usb = $usbDevices[$choice - 1]
} else {
    $usb = $usbDevices[0]
}

$model = Get-DeviceValue $usb.Serial @('shell','getprop','ro.product.model')
$hardwareSerial = Get-DeviceValue $usb.Serial @('shell','getprop','ro.serialno')
$sdkText = Get-DeviceValue $usb.Serial @('shell','getprop','ro.build.version.sdk')
$sdk = 0
if (-not [int]::TryParse($sdkText, [ref]$sdk)) { throw "Invalid Android SDK level: $sdkText" }

Write-Host "Target: $model (Android API $sdk)"
$legacyEndpoints = @()

if ($sdk -ge 30) {
    $existingWireless = $null
    foreach ($candidate in @(Get-ConnectedDevices | Where-Object Connection -eq 'wireless')) {
        try {
            $candidateIdentity = Get-DeviceValue $candidate.Serial @('shell','getprop','ro.serialno')
            if ($candidateIdentity -eq $hardwareSerial) {
                $existingWireless = $candidate
                break
            }
        } catch {}
    }

    if ($existingWireless) {
        Write-Host "Existing wireless pairing detected at $($existingWireless.Serial)."
    } else {
        Write-Host ''
        Write-Host 'On the phone open: Developer options > Wireless debugging.'
        Write-Host 'Choose: Pair device with pairing code.'
        $pairEndpoint = (Read-Host 'Enter the pairing IP address and port shown on the phone').Trim()
        if ($pairEndpoint -notmatch '^.+:[0-9]{1,5}$') { throw 'Invalid pairing endpoint.' }
        Write-Host 'ADB will ask for the six-digit pairing code.'
        & $adb pair $pairEndpoint
        if ($LASTEXITCODE -ne 0) { throw 'Wireless pairing failed.' }

        Start-Sleep -Seconds 1
        $services = Invoke-Adb @('mdns','services') -AllowFailure
        foreach ($line in $services) {
            if ($line -match '_adb-tls-connect\._tcp\s+([^\s]+:[0-9]+)\s*$') {
                Invoke-Adb @('connect',$Matches[1]) -AllowFailure | Out-Null
            }
        }
    }
    Write-Host 'Modern TLS wireless ADB pairing completed.'
} else {
    $addressOutput = Get-DeviceValue $usb.Serial @('shell','ip','-4','addr','show','wlan0')
    if ($addressOutput -notmatch 'inet\s+([0-9.]+)/') { throw 'The phone is not connected to Wi-Fi.' }
    $endpoint = "$($Matches[1]):5555"
    Invoke-Adb @('-s',$usb.Serial,'tcpip','5555') | ForEach-Object { Write-Host $_ }
    Start-Sleep -Seconds 1
    Invoke-Adb @('connect',$endpoint) | ForEach-Object { Write-Host $_ }
    $legacyEndpoints = @($endpoint)
    Write-Host 'Legacy TCP wireless ADB configured. It may need USB reactivation after a phone reboot.'
}

$config = [ordered]@{
    mode = 'auto'
    prefer_wireless = $true
    usb_fallback = $true
    preferred_hardware_serial = $hardwareSerial
    preferred_model = $model
    legacy_endpoints = $legacyEndpoints
}
Write-Config $config

Write-Host ''
Write-Host 'Configuration saved. Wireless is preferred and USB remains the fallback.'
Write-Host 'The dashboard watcher will switch transports automatically.'
Start-Sleep -Seconds 2
Show-Status

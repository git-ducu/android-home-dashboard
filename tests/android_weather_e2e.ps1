param(
    [string]$Device = '',
    [string]$DashboardUrl = 'http://127.0.0.1:8765/'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$adb = Join-Path $repoRoot 'runtime\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $adb)) { throw 'Local ADB runtime is missing.' }

$env:ADB_VENDOR_KEYS = Join-Path $repoRoot 'runtime\adb-keys\adbkey'
$env:ANDROID_USER_HOME = Join-Path $repoRoot 'runtime\android-home'

if (-not $Device) {
    $serials = & $adb devices | Select-String '\sdevice$' | ForEach-Object { ($_.Line -split '\s+')[0] }
    $Device = $serials | Where-Object { $_ -notlike '*:*' } | Select-Object -First 1
    if (-not $Device) { $Device = $serials | Select-Object -First 1 }
}
if (-not $Device) { throw 'No authorized Android device is connected.' }

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$debugPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$socket = $null
$nextId = 0

function Invoke-Cdp([string]$Method, [hashtable]$Params) {
    $script:nextId++
    $request = @{ id = $script:nextId; method = $Method; params = $Params } | ConvertTo-Json -Compress -Depth 20
    $bytes = [Text.Encoding]::UTF8.GetBytes($request)
    $null = $socket.SendAsync(
        [ArraySegment[byte]]::new($bytes),
        [Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()

    while ($true) {
        $stream = [IO.MemoryStream]::new()
        do {
            $buffer = New-Object byte[] 65536
            $received = $socket.ReceiveAsync(
                [ArraySegment[byte]]::new($buffer),
                [Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
            if ($received.Count) { $stream.Write($buffer, 0, $received.Count) }
        } while (-not $received.EndOfMessage)

        $message = [Text.Encoding]::UTF8.GetString($stream.ToArray()) | ConvertFrom-Json
        if ($message.id -eq $script:nextId) { return $message }
    }
}

function Invoke-JavaScript([string]$Expression) {
    $response = Invoke-Cdp 'Runtime.evaluate' @{
        expression = $Expression
        returnByValue = $true
        awaitPromise = $true
        userGesture = $true
    }
    if ($response.result.exceptionDetails) {
        $details = $response.result.exceptionDetails
        throw "$($details.text): $($details.exception.description)`nExpression: $Expression"
    }
    return $response.result.result.value
}

function Convert-Result([string]$Json) {
    if (-not $Json) { throw 'Android Chrome returned an empty test result.' }
    return $Json | ConvertFrom-Json
}

try {
    & $adb -s $Device forward "tcp:$debugPort" localabstract:chrome_devtools_remote | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not forward the Android Chrome DevTools socket.' }

    $targets = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$debugPort/json/list" -TimeoutSec 1
            if ($targets) { break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $targets) { throw 'Android Chrome DevTools endpoint did not become ready.' }

    $target = $targets | Where-Object { $_.type -eq 'page' -and $_.url -like "$DashboardUrl*" } | Select-Object -First 1
    if (-not $target) { throw "Open the Dashboard PWA in Android Chrome first: $DashboardUrl" }

    $debugUri = [UriBuilder]$target.webSocketDebuggerUrl
    $debugUri.Host = '127.0.0.1'
    $debugUri.Port = $debugPort
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $null = $socket.ConnectAsync($debugUri.Uri, [Threading.CancellationToken]::None).GetAwaiter().GetResult()

    $origin = ([uri]$DashboardUrl).GetLeftPart([UriPartial]::Authority)
    Invoke-Cdp 'Browser.grantPermissions' @{ origin = $origin; permissions = @('geolocation') } | Out-Null
    Invoke-Cdp 'Page.reload' @{ ignoreCache = $true } | Out-Null
    Start-Sleep -Seconds 2

    $live = Convert-Result (Invoke-JavaScript @'
(async () => {
  presentation.showPage('today', {user: true});
  const deadline = Date.now() + 15000;
  while ((weatherLoading || weatherGpsPromise) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
  const nativeFetch = window.fetch;
  const directRequests = [];
  const reverseRequests = [];
  localStorage.removeItem(WEATHER_PLACE_KEY);
  window.fetch = (...args) => {
    const url = String(args[0]?.url || args[0]);
    if (url.startsWith(WEATHER_API)) directRequests.push(url);
    if (url.startsWith(WEATHER_REVERSE_API)) reverseRequests.push(url);
    return nativeFetch(...args);
  };
  try {
    await refreshWeatherFromGps(true, false);
  } finally {
    window.fetch = nativeFetch;
  }
  const weatherPanel = document.querySelector('.weather-panel').getBoundingClientRect();
  return JSON.stringify({
    directRequests: directRequests.length,
    reverseRequests: reverseRequests.length,
    temperature: document.getElementById('weatherNowTemp').textContent,
    hourly: document.querySelectorAll('#weatherHourly .weather-slot').length,
    tomorrow: document.querySelectorAll('#weatherTomorrow .weather-slot').length,
    location: document.getElementById('weatherLocationName').textContent,
    gpsSource: weatherLocation.source,
    latitude: weatherLocation.latitude,
    longitude: weatherLocation.longitude,
    gpsState: document.getElementById('weatherGpsStatus').dataset.state,
    namedPlace: !/^-?\d/.test(document.getElementById('weatherLocationName').textContent),
    cache: Boolean(localStorage.getItem(WEATHER_CACHE_KEY)),
    viewport: `${innerWidth}x${innerHeight}`,
    overflow: document.documentElement.scrollWidth > innerWidth + 1,
    panelVisible: weatherPanel.left >= 0 && weatherPanel.right <= innerWidth + 1 && weatherPanel.height > 0
  });
})()
'@)
    if ($live.directRequests -lt 1 -or $live.reverseRequests -lt 1 -or $live.temperature -eq '--' -or $live.hourly -ne 6 -or
        $live.tomorrow -ne 4 -or -not $live.location -or $live.gpsSource -ne 'gps' -or
        $live.gpsState -ne 'active' -or -not $live.namedPlace -or -not $live.latitude -or
        -not $live.longitude -or -not $live.cache -or
        $live.overflow -or -not $live.panelVisible) {
        throw 'Phone-direct weather rendering or A51 layout validation failed.'
    }

    $cached = Convert-Result (Invoke-JavaScript @'
(async () => {
  const nativeFetch = window.fetch;
  window.fetch = (...args) => {
    const url = String(args[0]?.url || args[0]);
    if (url.startsWith(WEATHER_API)) return Promise.reject(new TypeError('intentional offline test'));
    return nativeFetch(...args);
  };
  try {
    await updateWeather(true);
  } finally {
    window.fetch = nativeFetch;
  }
  return JSON.stringify({
    temperature: document.getElementById('weatherNowTemp').textContent,
    hourly: document.querySelectorAll('#weatherHourly .weather-slot').length,
    tomorrow: document.querySelectorAll('#weatherTomorrow .weather-slot').length,
    cachedFallback: document.getElementById('weatherUpdated').textContent.startsWith('\uC800\uC7A5\uB428')
  });
})()
'@)
    if ($cached.temperature -eq '--' -or $cached.hourly -ne 6 -or $cached.tomorrow -ne 4 -or -not $cached.cachedFallback) {
        throw 'Cached weather fallback did not remain usable while offline.'
    }

    [pscustomobject]@{
        Device = $Device
        AndroidChromeStatus = 'ready'
        Viewport = $live.viewport
        Location = $live.location
        GpsCoordinates = "$($live.latitude),$($live.longitude)"
        Temperature = $live.temperature
        DirectForecastRequest = 'passed'
        ReverseGeocoding = 'passed'
        SixHourForecast = 'passed'
        TomorrowForecast = 'passed'
        OfflineCache = 'passed'
    } | Format-List
} finally {
    if ($socket -and $socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        $null = $socket.CloseAsync(
            [Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
            'done',
            [Threading.CancellationToken]::None
        ).GetAwaiter().GetResult()
    }
    & $adb -s $Device forward --remove "tcp:$debugPort" 2>$null | Out-Null
}

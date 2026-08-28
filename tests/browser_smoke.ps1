param(
    [string]$BaseUrl = 'http://127.0.0.1:8765/'
)

$ErrorActionPreference = 'Stop'
$edgeCandidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)
$edge = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $edge) { throw 'Microsoft Edge is required for the browser smoke test.' }

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$debugPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$validationRoot = Join-Path ([IO.Path]::GetTempPath()) ('android-home-dashboard-cdp-' + [guid]::NewGuid().ToString('N'))
$profileDir = Join-Path $validationRoot 'profile'
$todayScreenshotPath = Join-Path $validationRoot 'today.png'
$screenshotPath = Join-Path $validationRoot 'now-playing.png'
$settingsScreenshotPath = Join-Path $validationRoot 'settings.png'
New-Item -ItemType Directory -Path $profileDir | Out-Null

$browser = $null
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
    $result = Invoke-Cdp 'Runtime.evaluate' @{ expression = $Expression; returnByValue = $true; awaitPromise = $true }
    if ($result.result.exceptionDetails) {
        $description = $result.result.exceptionDetails.exception.description
        throw "$($result.result.exceptionDetails.text): $description`nExpression: $Expression"
    }
    return $result.result.result.value
}

try {
    $browser = Start-Process -FilePath $edge -ArgumentList @(
        '--headless=new',
        '--disable-gpu',
        '--hide-scrollbars',
        '--window-size=1080,480',
        "--remote-debugging-port=$debugPort",
        "--user-data-dir=$profileDir",
        $BaseUrl
    ) -WindowStyle Hidden -PassThru

    $target = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$debugPort/json/list" -TimeoutSec 1
            $target = $targets | Where-Object { $_.type -eq 'page' -and $_.url -like "$BaseUrl*" } | Select-Object -First 1
            if ($target) { break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $target.webSocketDebuggerUrl) { throw 'Dashboard page target was not found.' }
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $null = $socket.ConnectAsync([uri]$target.webSocketDebuggerUrl, [Threading.CancellationToken]::None).GetAwaiter().GetResult()

    $origin = ([uri]$BaseUrl).GetLeftPart([UriPartial]::Authority)
    Invoke-Cdp 'Browser.grantPermissions' @{ origin = $origin; permissions = @('geolocation') } | Out-Null
    Invoke-Cdp 'Emulation.setGeolocationOverride' @{ latitude = 37.5665; longitude = 126.9780; accuracy = 25 } | Out-Null

    $appReady = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $appReady = Invoke-JavaScript 'typeof presentation === "object" && Array.isArray(PAGE_REGISTRY)'
        if ($appReady) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $appReady) { throw 'Dashboard JavaScript did not become ready.' }
    $ready = Invoke-JavaScript 'JSON.stringify({controller:typeof presentation,page:presentation?.pageId||null,status:document.getElementById("conn")?.textContent||null,pageIds:PAGE_REGISTRY.map(item=>item.id)})'
    $pendingIndicator = Invoke-JavaScript '(()=>{const original=presentation.latestContext;presentation.latestContext={state:"WORK",pending:{state:"VIDEO",phase:"entering",remaining_ms:1800},detectors:{GAME:{raw:false},VIDEO:{raw:true},IDLE:{raw:false},WORK:{raw:true}}};presentation.mode="AUTO";presentation.updateMode();const label=document.getElementById("contextState"),result={text:label.textContent,pending:label.classList.contains("pending")};presentation.latestContext=original;presentation.updateMode();return JSON.stringify(result)})()'
    Invoke-JavaScript 'window.__clipMutations=0;window.__smokeErrors=[];window.addEventListener("error",event=>window.__smokeErrors.push(String(event.message)));window.addEventListener("unhandledrejection",event=>window.__smokeErrors.push(String(event.reason)));new MutationObserver(records=>window.__clipMutations+=records.filter(record=>record.type==="childList").length).observe(document.getElementById("clipList"),{childList:true,subtree:true});true' | Out-Null
    Start-Sleep -Seconds 3
    $clipboard = Invoke-JavaScript 'JSON.stringify({mutations:window.__clipMutations,items:document.querySelectorAll("#clipList .clip-item").length,images:document.querySelectorAll("#clipList img").length})'

    $today = Invoke-JavaScript @'
(async () => {
  presentation.showPage("today", {user: true});
  const deadline = Date.now() + 15000;
  while ((weatherLoading || weatherGpsPromise) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
  const now = new Date();
  now.setMinutes(0, 0, 0);
  const hourlyTime = Array.from({length: 60}, (_, index) => {
    const value = new Date(now.getTime() + index * 60 * 60 * 1000);
    return `${localDateKey(value)}T${pad2(value.getHours())}:00`;
  });
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const weatherData = {
    current: {temperature_2m: 24, apparent_temperature: 25, relative_humidity_2m: 58, weather_code: 1, wind_speed_10m: 7, is_day: 1},
    hourly: {
      time: hourlyTime,
      temperature_2m: hourlyTime.map((_, index) => 24 - Math.floor(index / 5)),
      precipitation_probability: hourlyTime.map((_, index) => index % 7 === 0 ? 20 : 5),
      weather_code: hourlyTime.map((_, index) => index % 7 === 0 ? 2 : 1),
      is_day: hourlyTime.map(value => { const hour = new Date(value).getHours(); return hour >= 6 && hour < 19 ? 1 : 0; })
    },
    daily: {
      time: [localDateKey(now), localDateKey(tomorrow)],
      temperature_2m_min: [18, 19],
      temperature_2m_max: [27, 28],
      precipitation_probability_max: [20, 30]
    }
  };
  saveWeatherLocation({name: "\uC11C\uC6B8\uD2B9\uBCC4\uC2DC", latitude: 37.5665, longitude: 126.9780, accuracy: 25, timezone: "Asia/Seoul"});
  saveWeatherPlaceName("\uC11C\uC6B8\uD2B9\uBCC4\uC2DC");
  setWeatherGpsStatus("active", "GPS DEMO - 25m");
  renderWeather(weatherData, Date.now(), false);
  localStorage.setItem(WEATHER_CACHE_KEY, JSON.stringify({location_id: weatherLocationId(), fetched_at: Date.now(), data: weatherData}));
  return JSON.stringify({
    page: presentation.pageId,
    days: document.querySelectorAll("#calendarDays .cal-day").length,
    hourly: document.querySelectorAll("#weatherHourly .weather-slot").length,
    tomorrow: document.querySelectorAll("#weatherTomorrow .weather-slot").length,
    location: document.getElementById("weatherLocationName").textContent,
    temperature: document.getElementById("weatherNowTemp").textContent,
    cached: Boolean(localStorage.getItem(WEATHER_CACHE_KEY)),
    gps: weatherLocation.source === "gps" && Number.isFinite(weatherLocation.latitude) && Number.isFinite(weatherLocation.longitude),
    gpsState: document.getElementById("weatherGpsStatus").dataset.state,
    namedPlace: !/^-?\d/.test(document.getElementById("weatherLocationName").textContent)
  });
})()
'@
    Invoke-JavaScript 'window.__liveRender=render;render=data=>{window.__liveRender(data);setText("activeTitle","Visual Studio Code");setText("activeProc","Code.exe - DEMO")};render(latestStatus);true' | Out-Null
    Start-Sleep -Milliseconds 400
    $todayCapture = Invoke-Cdp 'Page.captureScreenshot' @{ format = 'png'; fromSurface = $true; captureBeyondViewport = $false }
    [IO.File]::WriteAllBytes($todayScreenshotPath, [Convert]::FromBase64String($todayCapture.result.data))
    $nowPlaying = Invoke-JavaScript 'presentation.showPage("now_playing",{user:true});JSON.stringify({page:presentation.pageId,mode:presentation.mode,state:document.getElementById("mediaPage").dataset.mediaState,title:document.getElementById("song").textContent,controls:[...document.querySelectorAll(".media-controls button")].map(button=>button.textContent)})'
    Start-Sleep -Milliseconds 400
    $capture = Invoke-Cdp 'Page.captureScreenshot' @{ format = 'png'; fromSurface = $true; captureBeyondViewport = $false }
    [IO.File]::WriteAllBytes($screenshotPath, [Convert]::FromBase64String($capture.result.data))
    $mediaEmpty = Invoke-JavaScript 'renderMedia({available:false});JSON.stringify({state:document.getElementById("mediaPage").dataset.mediaState,activeDisplay:getComputedStyle(document.querySelector(".media-active")).display,emptyDisplay:getComputedStyle(document.querySelector(".media-empty")).display,launchButtons:document.querySelectorAll(".media-launch button").length})'
    Invoke-JavaScript 'renderMedia(latestStatus.media);true' | Out-Null

    $settingsOpen = Invoke-JavaScript 'window.__closeTokenBefore=presentation.contextToken;document.getElementById("settingsOpen").click();JSON.stringify({open:document.getElementById("settingsOverlay").classList.contains("on"),mode:presentation.mode})'
    Start-Sleep -Milliseconds 400
    $settingsCapture = Invoke-Cdp 'Page.captureScreenshot' @{ format = 'png'; fromSurface = $true; captureBeyondViewport = $false }
    [IO.File]::WriteAllBytes($settingsScreenshotPath, [Convert]::FromBase64String($settingsCapture.result.data))
    $settingsClose = Invoke-JavaScript 'document.getElementById("settingsClose").click();JSON.stringify({open:document.getElementById("settingsOverlay").classList.contains("on"),page:presentation.pageId,mode:presentation.mode,tokenBefore:window.__closeTokenBefore,tokenAfter:presentation.contextToken})'
    $settingsSaveStart = Invoke-JavaScript 'window.__settingsTokenBefore=presentation.contextToken;window.__settingsExpectedPage=(automationConfig.profiles[latestStatus.context.state]||automationConfig.profiles.DEFAULT).pages[0];document.getElementById("settingsOpen").click();document.getElementById("settingsSave").click();true'
    Start-Sleep -Seconds 1
    $settingsSave = Invoke-JavaScript 'JSON.stringify({open:document.getElementById("settingsOverlay").classList.contains("on"),page:presentation.pageId,mode:presentation.mode,expectedPage:window.__settingsExpectedPage,tokenBefore:window.__settingsTokenBefore,tokenAfter:presentation.contextToken})'
    $resumeAuto = Invoke-JavaScript 'document.getElementById("resumeAuto").click();JSON.stringify({page:presentation.pageId,mode:presentation.mode})'
    $swipeGestures = Invoke-JavaScript '(()=>{const originalHandleContext=presentation.handleContext;presentation.handleContext=()=>{};presentation.showPage("game_performance",{user:true});const button=document.querySelector(''[data-page-id="game_performance"] .volume button'');let postSwipeClicks=0;const countClick=()=>postSwipeClicks++;button.addEventListener("click",countClick);button.dispatchEvent(new MouseEvent("mousedown",{bubbles:true,clientX:800,clientY:200}));button.dispatchEvent(new MouseEvent("mouseup",{bubbles:true,clientX:600,clientY:205}));button.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true}));const actionButtonSwipe={page:presentation.pageId,mode:presentation.mode,postSwipeClicks};const system=document.querySelector(".performance");system.dispatchEvent(new MouseEvent("mousedown",{bubbles:true,clientX:100,clientY:200}));system.dispatchEvent(new MouseEvent("mouseup",{bubbles:true,clientX:300,clientY:205}));const systemPanelSwipe={page:presentation.pageId,mode:presentation.mode};button.removeEventListener("click",countClick);presentation.handleContext=originalHandleContext;return JSON.stringify({actionButtonSwipe,systemPanelSwipe})})()'
    $rotationStart = Invoke-JavaScript 'window.__originalHandleContext=presentation.handleContext.bind(presentation);presentation.handleContext=()=>{};presentation.mode="AUTO";presentation.applyProfile({pages:["clipboard","diagnostics"],rotation:{enabled:true,interval_sec:5}},true);JSON.stringify({page:presentation.pageId,mode:presentation.mode})'
    $rotationAdvance = $null
    for ($attempt = 0; $attempt -lt 32; $attempt++) {
        Start-Sleep -Milliseconds 250
        $rotationAdvance = Invoke-JavaScript 'JSON.stringify({page:presentation.pageId,mode:presentation.mode})'
        if (($rotationAdvance | ConvertFrom-Json).page -eq 'diagnostics') { break }
    }
    $manualStart = Invoke-JavaScript 'presentation.showPage("clipboard",{user:true});JSON.stringify({page:presentation.pageId,mode:presentation.mode})'
    Start-Sleep -Milliseconds 5400
    $manualHold = Invoke-JavaScript '(()=>{const result={page:presentation.pageId,mode:presentation.mode};if(window.__originalHandleContext){presentation.handleContext=window.__originalHandleContext;delete window.__originalHandleContext}return JSON.stringify(result)})()'
    $errors = Invoke-JavaScript 'JSON.stringify(window.__smokeErrors)'

    $clipboardResult = $clipboard | ConvertFrom-Json
    $pendingResult = $pendingIndicator | ConvertFrom-Json
    $todayResult = $today | ConvertFrom-Json
    $nowPlayingResult = $nowPlaying | ConvertFrom-Json
    $mediaEmptyResult = $mediaEmpty | ConvertFrom-Json
    $settingsCloseResult = $settingsClose | ConvertFrom-Json
    $settingsSaveResult = $settingsSave | ConvertFrom-Json
    $resumeResult = $resumeAuto | ConvertFrom-Json
    $swipeResult = $swipeGestures | ConvertFrom-Json
    $rotationResult = $rotationAdvance | ConvertFrom-Json
    $manualResult = $manualHold | ConvertFrom-Json
    $errorResult = $errors | ConvertFrom-Json
    $readyResult = $ready | ConvertFrom-Json
    if ($readyResult.pageIds -notcontains 'today' -or $todayResult.page -ne 'today' -or $todayResult.days -ne 42 -or $todayResult.hourly -ne 6 -or $todayResult.tomorrow -ne 4 -or -not $todayResult.location -or $todayResult.temperature -eq '--' -or -not $todayResult.cached -or -not $todayResult.gps -or $todayResult.gpsState -ne 'active' -or -not $todayResult.namedPlace) { throw 'TODAY GPS place name, status, or phone-direct forecast is incomplete.' }
    if ($clipboardResult.mutations -ne 0) { throw "Clipboard DOM changed $($clipboardResult.mutations) times without new content." }
    if (-not $pendingResult.pending -or $pendingResult.text -notlike 'WORK*VIDEO*1.8s') { throw 'Context transition countdown is not visible.' }
    if ($nowPlayingResult.controls.Count -ne 3) { throw 'Now Playing must expose exactly three playback controls.' }
    if ($mediaEmptyResult.state -ne 'empty' -or $mediaEmptyResult.activeDisplay -ne 'none' -or $mediaEmptyResult.emptyDisplay -ne 'grid' -or $mediaEmptyResult.launchButtons -ne 2) { throw 'Now Playing empty state layout is incorrect.' }
    if ($settingsCloseResult.open) { throw 'Closing Settings did not close the overlay.' }
    if ($settingsCloseResult.tokenBefore -eq $settingsCloseResult.tokenAfter -and ($settingsCloseResult.mode -ne 'MANUAL' -or $settingsCloseResult.page -ne 'now_playing')) { throw 'Closing Settings did not preserve the manual page.' }
    if ($settingsSaveResult.open) { throw 'Saving Settings did not close the overlay.' }
    if ($settingsSaveResult.mode -ne 'AUTO' -or $settingsSaveResult.page -ne $settingsSaveResult.expectedPage) { throw 'Saving Settings did not immediately apply the active profile.' }
    if ($resumeResult.mode -ne 'AUTO') { throw 'Resume Auto did not restore automatic mode.' }
    if ($swipeResult.actionButtonSwipe.page -ne 'diagnostics' -or $swipeResult.actionButtonSwipe.mode -ne 'MANUAL') { throw 'Swipe starting on an action button did not advance to Diagnostics.' }
    if ($swipeResult.actionButtonSwipe.postSwipeClicks -ne 0) { throw 'Action button click fired after a swipe.' }
    if ($swipeResult.systemPanelSwipe.page -ne 'game_performance' -or $swipeResult.systemPanelSwipe.mode -ne 'MANUAL') { throw 'Swipe starting on the System panel did not return to Game Performance.' }
    if ($rotationResult.mode -ne 'AUTO' -or $rotationResult.page -ne 'diagnostics') { throw 'Automatic page rotation did not advance.' }
    if ($manualResult.mode -ne 'MANUAL' -or $manualResult.page -ne 'clipboard') { throw 'Manual override did not stop automatic rotation.' }
    if ($errorResult.Count -gt 0) { throw "Browser errors: $($errorResult -join '; ')" }

    [pscustomobject]@{
        Ready = $ready
        PendingIndicator = $pendingIndicator
        Clipboard = $clipboard
        Today = $today
        NowPlaying = $nowPlaying
        MediaEmpty = $mediaEmpty
        SettingsOpen = $settingsOpen
        SettingsClose = $settingsClose
        SettingsSave = $settingsSave
        ResumeAuto = $resumeAuto
        SwipeGestures = $swipeGestures
        RotationStart = $rotationStart
        RotationAdvance = $rotationAdvance
        ManualStart = $manualStart
        ManualHold = $manualHold
        TodayScreenshot = $todayScreenshotPath
        Screenshot = $screenshotPath
        SettingsScreenshot = $settingsScreenshotPath
    } | Format-List
} finally {
    if ($socket -and $socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        $null = $socket.CloseAsync([Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'done', [Threading.CancellationToken]::None).GetAwaiter().GetResult()
    }
    if ($browser -and -not $browser.HasExited) {
        Stop-Process -Id $browser.Id -Force -ErrorAction SilentlyContinue
        $browser.WaitForExit(5000) | Out-Null
    }
    $fullValidationRoot = [IO.Path]::GetFullPath($validationRoot).TrimEnd('\')
    $fullProfileDir = [IO.Path]::GetFullPath($profileDir)
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\android-home-dashboard-cdp-'
    if ($fullValidationRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $fullProfileDir.StartsWith($fullValidationRoot + '\', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $fullProfileDir)) {
        Remove-Item -LiteralPath $fullProfileDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

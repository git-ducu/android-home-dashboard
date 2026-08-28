$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $scriptDir '..\..')).Path
$runtime = Join-Path $root 'runtime'
$staging = Join-Path $runtime '.staging'
$pythonDir = Join-Path $runtime 'python'
$adbDir = Join-Path $runtime 'platform-tools'
$presentMonDir = Join-Path $runtime 'presentmon'
$adbKeyDir = Join-Path $runtime 'adb-keys'
$androidHome = Join-Path $runtime 'android-home'

$pythonVersion = '3.13.14'
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
$pythonSha256 = '90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907'
$getPipUrl = 'https://bootstrap.pypa.io/get-pip.py'
$adbUrl = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
$presentMonUrl = 'https://github.com/GameTechDev/PresentMon/releases/download/v2.5.1/PresentMon-2.5.1-x64.exe'

function Download-File([string]$Uri, [string]$Destination) {
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -Uri $Uri -OutFile $Destination -UseBasicParsing
}

function Assert-ExitCode([int]$Code, [string]$Step) {
    if ($Code -ne 0) { throw "$Step failed with exit code $Code" }
}

New-Item -ItemType Directory -Path $runtime -Force | Out-Null
if (Test-Path $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$env:PIP_CACHE_DIR = Join-Path $staging 'pip-cache'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:ANDROID_USER_HOME = $androidHome
$env:ADB_VENDOR_KEYS = Join-Path $adbKeyDir 'adbkey'

try {
    $python = Join-Path $pythonDir 'python.exe'
    if (-not (Test-Path $python)) {
        Write-Host '[1/5] Portable Python'
        $zip = Join-Path $staging 'python.zip'
        Download-File $pythonUrl $zip
        $actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $pythonSha256) { throw 'Python archive checksum mismatch.' }
        New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
        Expand-Archive -LiteralPath $zip -DestinationPath $pythonDir -Force
        $pth = Get-ChildItem -LiteralPath $pythonDir -Filter 'python*._pth' | Select-Object -First 1
        if (-not $pth) { throw 'Python path configuration was not found.' }
        $content = Get-Content -LiteralPath $pth.FullName
        $content = $content -replace '^#import site$', 'import site'
        if ($content -notcontains 'Lib\site-packages') {
            $siteIndex = [Array]::IndexOf($content, 'import site')
            if ($siteIndex -lt 0) { $siteIndex = $content.Count }
            $content = @($content[0..($siteIndex - 1)]) + 'Lib\site-packages' + @($content[$siteIndex..($content.Count - 1)])
        }
        Set-Content -LiteralPath $pth.FullName -Value $content -Encoding ASCII
    }

    Write-Host '[2/5] Python packages'
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & $python -m pip --version 2>$null | Out-Null
    $pipAvailable = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previousErrorAction
    if (-not $pipAvailable) {
        $getPip = Join-Path $staging 'get-pip.py'
        Download-File $getPipUrl $getPip
        & $python $getPip --disable-pip-version-check --no-cache-dir --no-warn-script-location
        Assert-ExitCode $LASTEXITCODE 'pip bootstrap'
    }
    & $python -m pip install --disable-pip-version-check --no-cache-dir --no-warn-script-location --upgrade psutil winrt-runtime==3.2.1 winrt-Windows.Foundation==3.2.1 winrt-Windows.Foundation.Collections==3.2.1 winrt-Windows.Media.Control==3.2.1 winrt-Windows.Storage.Streams==3.2.1
    Assert-ExitCode $LASTEXITCODE 'Python package installation'

    Write-Host '[3/5] Android Platform Tools'
    $adb = Join-Path $adbDir 'adb.exe'
    if (-not (Test-Path $adb)) {
        $zip = Join-Path $staging 'platform-tools.zip'
        $expanded = Join-Path $staging 'adb'
        Download-File $adbUrl $zip
        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force
        $source = Join-Path $expanded 'platform-tools'
        if (-not (Test-Path (Join-Path $source 'adb.exe'))) { throw 'ADB archive is invalid.' }
        if (Test-Path $adbDir) { Remove-Item -LiteralPath $adbDir -Recurse -Force }
        Move-Item -LiteralPath $source -Destination $adbDir
    }
    New-Item -ItemType Directory -Path $adbKeyDir,$androidHome -Force | Out-Null
    $adbKey = Join-Path $adbKeyDir 'adbkey'
    if (-not (Test-Path $adbKey)) {
        $existingKey = Join-Path $env:USERPROFILE '.android\adbkey'
        if (Test-Path $existingKey) {
            Copy-Item -LiteralPath $existingKey -Destination $adbKey
            if (Test-Path "$existingKey.pub") { Copy-Item -LiteralPath "$existingKey.pub" -Destination "$adbKey.pub" }
        } else {
            & $adb keygen $adbKey
            Assert-ExitCode $LASTEXITCODE 'ADB key generation'
        }
    }

    Write-Host '[4/5] PresentMon Console'
    $presentMon = Join-Path $presentMonDir 'PresentMon.exe'
    if (-not (Test-Path $presentMon)) {
        New-Item -ItemType Directory -Path $presentMonDir -Force | Out-Null
        Download-File $presentMonUrl $presentMon
    }

    Write-Host '[5/5] Permissions and scheduled tasks'
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & net.exe localgroup 'Performance Log Users' $env:USERNAME /add 2>&1 | Out-Null
    $groupExitCode = $LASTEXITCODE
    if ($groupExitCode -ne 0) {
        $members = (& net.exe localgroup 'Performance Log Users' 2>&1) -join "`n"
        if ($members -notmatch [regex]::Escape($env:USERNAME)) {
            throw 'Failed to grant PresentMon access.'
        }
    }
    $ErrorActionPreference = $previousErrorAction

    & $python -B -c "import psutil; from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager; print('Local Python runtime OK')"
    Assert-ExitCode $LASTEXITCODE 'Local Python verification'
    & $adb version | Select-Object -First 1
    Assert-ExitCode $LASTEXITCODE 'Local ADB verification'
    if ((Get-Item $presentMon).Length -lt 500KB) { throw 'PresentMon download is incomplete.' }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\runtime\dashboard_task.ps1') install
    Assert-ExitCode $LASTEXITCODE 'Scheduled task registration'

    Get-ChildItem -LiteralPath $runtime -Directory -Filter '__pycache__' -Recurse -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
finally {
    if (Test-Path $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
}

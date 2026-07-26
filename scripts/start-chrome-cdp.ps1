# Start browser with CDP for captcha.
# Default: Microsoft Edge + real Edge User Data (hand-verified pass on this machine).
# Loads CAPTCHA_* from project .env when present.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start-chrome-cdp.ps1
#   powershell -File scripts\start-chrome-cdp.ps1 -ForceRestart

param([switch]$ForceRestart)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Load CAPTCHA_* from .env
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $i = $line.IndexOf("=")
        if ($i -lt 1) { return }
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim()
        if ($k -like "CAPTCHA_*") {
            [Environment]::SetEnvironmentVariable($k, $v, "Process")
        }
    }
}

$Port = if ($env:CAPTCHA_CHROME_DEBUG_PORT) { [int]$env:CAPTCHA_CHROME_DEBUG_PORT } else { 9222 }
$CdpUrl = if ($env:CAPTCHA_CHROME_CDP_URL) { $env:CAPTCHA_CHROME_CDP_URL.TrimEnd('/') } else { "http://127.0.0.1:$Port" }
$BrowserPref = if ($env:CAPTCHA_BROWSER) { $env:CAPTCHA_BROWSER.Trim().ToLower() } else { "edge" }

function Test-CdpReady {
    try {
        $r = Invoke-WebRequest -Uri "$CdpUrl/json/version" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -ge 200 -and $r.StatusCode -lt 300
    } catch {
        return $false
    }
}

if ((-not $ForceRestart) -and (Test-CdpReady)) {
    Write-Host "[OK] CDP already ready: $CdpUrl"
    exit 0
}

# Resolve browser executable: Edge preferred (hand-pass env)
$edgeCandidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
)
$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

$BrowserExe = $null
if ($env:CAPTCHA_CHROME_PATH -and (Test-Path $env:CAPTCHA_CHROME_PATH)) {
    $BrowserExe = $env:CAPTCHA_CHROME_PATH
} elseif ($BrowserPref -eq "chrome") {
    $BrowserExe = $chromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
} else {
    $BrowserExe = $edgeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $BrowserExe) {
        $BrowserExe = $chromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
}
if (-not $BrowserExe) {
    Write-Host "[ERROR] Edge/Chrome executable not found"
    exit 1
}

$IsEdge = ($BrowserExe -match 'msedge\.exe$')
$DefaultEdgeData = Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data"
$DefaultChromeData = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
$ProjectEdge = Join-Path $Root "browser_data\edge_manual"

# Prefer env override; else real Edge User Data (the one that passed manually)
if ($env:CAPTCHA_CHROME_USER_DATA_DIR -and $env:CAPTCHA_CHROME_USER_DATA_DIR.Trim()) {
    $UserData = $env:CAPTCHA_CHROME_USER_DATA_DIR.Trim()
} elseif ($IsEdge -and (Test-Path $DefaultEdgeData)) {
    $UserData = $DefaultEdgeData
} elseif ($IsEdge) {
    $UserData = $ProjectEdge
} else {
    $UserData = $DefaultChromeData
}
$Profile = if ($env:CAPTCHA_CHROME_PROFILE) { $env:CAPTCHA_CHROME_PROFILE } else { "Default" }

Write-Host "Stopping browser for CDP restart..."
# Kill processes using our debug port / user data / common browsers
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(msedge|chrome)\.exe$' -and $_.CommandLine -and (
        $_.CommandLine -match "remote-debugging-port=$Port" -or
        $_.CommandLine -match [regex]::Escape($UserData) -or
        $_.CommandLine -match 'chrome_env_v3|chrome_clean_manual|edge_manual'
    )
} | ForEach-Object {
    cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
}
if ($ForceRestart -or $IsEdge) {
    # Real Edge profile requires no other Edge instances holding the lock
    Get-Process msedge, chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    cmd /c "taskkill /IM msedge.exe /F >nul 2>&1"
    cmd /c "taskkill /IM chrome.exe /F >nul 2>&1"
    Start-Sleep -Seconds 1
}

if (-not (Test-Path $UserData)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $UserData $Profile) | Out-Null
}

Write-Host "Starting browser for captcha CDP..."
Write-Host "  exe:     $BrowserExe"
Write-Host "  kind:    $(if ($IsEdge) { 'Edge' } else { 'Chrome' })"
Write-Host "  port:    $Port"
Write-Host "  data:    $UserData"
Write-Host "  profile: $Profile"

# Minimal flags — match a normal desktop Edge session as much as possible
$args = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$UserData",
    "--profile-directory=$Profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--lang=zh-CN",
    "--start-maximized"
)

$proxy = if ($env:CAPTCHA_CHROME_PROXY) { $env:CAPTCHA_CHROME_PROXY.Trim() } else { "" }
if ($proxy -and $proxy -notin @("direct","none","off","false","0","")) {
    $proxyServer = $proxy
    if ($proxy -match '^socks5h?://') {
        $u = [Uri]$proxy
        $proxyServer = "socks5://$($u.Host):$($u.Port)"
    } elseif ($proxy -match '^https?://') {
        $u = [Uri]$proxy
        $proxyServer = "http://$($u.Host):$($u.Port)"
    }
    $args += "--proxy-server=$proxyServer"
    $args += "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1"
    Write-Host "  proxy:   $proxyServer"
} else {
    Write-Host "  proxy:   system/default (本机直连)"
}

Start-Process -FilePath $BrowserExe -ArgumentList $args

for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Milliseconds 250
    if (Test-CdpReady) {
        try {
            $ver = (Invoke-WebRequest -Uri "$CdpUrl/json/version" -UseBasicParsing -TimeoutSec 2).Content
            Write-Host "[OK] CDP ready: $CdpUrl"
            Write-Host "  version: $ver"
        } catch {
            Write-Host "[OK] CDP ready: $CdpUrl"
        }
        exit 0
    }
}

Write-Host "[ERROR] Browser started but CDP port $Port not ready."
exit 3

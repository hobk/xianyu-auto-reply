[CmdletBinding()]
param([switch]$ForceRestart)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$sessionId = (Get-Process -Id $PID).SessionId
if ($sessionId -eq 0) {
    throw "Edge CDP must run in the logged-in desktop session, not Session 0."
}

$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) { return }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($name -like "CAPTCHA_*") { Set-Item -Path "Env:$name" -Value $value }
    }
}

$port = if ($env:CAPTCHA_CHROME_DEBUG_PORT) { [int]$env:CAPTCHA_CHROME_DEBUG_PORT } else { 9222 }
$cdpUrl = if ($env:CAPTCHA_CHROME_CDP_URL) { $env:CAPTCHA_CHROME_CDP_URL.TrimEnd('/') } else { "http://127.0.0.1:$port" }

function Test-CdpReady {
    try {
        $response = Invoke-WebRequest -Uri "$cdpUrl/json/version" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

if ((-not $ForceRestart) -and (Test-CdpReady)) {
    Write-Host "CDP already ready: $cdpUrl"
    exit 0
}

$browserCandidates = @(
    $env:CAPTCHA_CHROME_PATH,
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { $_ -and (Test-Path $_) }
$browserExe = $browserCandidates | Select-Object -First 1
if (-not $browserExe) { throw "Microsoft Edge was not found." }

$userData = if ($env:CAPTCHA_CHROME_USER_DATA_DIR) {
    $env:CAPTCHA_CHROME_USER_DATA_DIR.Trim()
} else {
    Join-Path $Root "browser_data\edge_manual"
}
$profile = if ($env:CAPTCHA_CHROME_PROFILE) { $env:CAPTCHA_CHROME_PROFILE.Trim() } else { "Default" }

# Only stop this project's CDP process. Do not touch other browser windows.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "msedge.exe" -and $_.CommandLine -and (
        $_.CommandLine -match "remote-debugging-port=$port" -or
        $_.CommandLine -match [regex]::Escape($userData)
    )
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($ForceRestart) { Start-Sleep -Seconds 1 }

New-Item -ItemType Directory -Force -Path (Join-Path $userData $profile) | Out-Null
$arguments = @(
    "--remote-debugging-port=$port",
    "--user-data-dir=`"$userData`"",
    "--profile-directory=`"$profile`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--lang=zh-CN",
    "--start-maximized"
)
if ($env:CAPTCHA_CHROME_PROXY -and $env:CAPTCHA_CHROME_PROXY.Trim() -notin @("", "direct", "none", "off", "false", "0")) {
    $arguments += "--proxy-server=$($env:CAPTCHA_CHROME_PROXY.Trim())"
    $arguments += "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1"
}

Write-Host "Starting Edge CDP in Session $sessionId on $cdpUrl"
Start-Process -FilePath $browserExe -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 250
    if (Test-CdpReady) {
        Write-Host "CDP ready: $cdpUrl"
        exit 0
    }
}
throw "Edge started but CDP did not become ready: $cdpUrl"

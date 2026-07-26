# Keep Edge/Chrome CDP alive for captcha (PM2 managed).
# Reads active profile from run/captcha_profile_path.txt (written by profile_pool).
# Does NOT kill a healthy CDP; restarts only when port is down or profile path changes.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile = Join-Path $Root ".env"
$Port = 9222
$CdpUrl = "http://127.0.0.1:$Port"
$CheckSec = 8

# Load CAPTCHA_* from .env
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
if ($env:CAPTCHA_CHROME_DEBUG_PORT) { $Port = [int]$env:CAPTCHA_CHROME_DEBUG_PORT }
if ($env:CAPTCHA_CHROME_CDP_URL) { $CdpUrl = $env:CAPTCHA_CHROME_CDP_URL.TrimEnd('/') }

$edgeCandidates = @(
    $env:CAPTCHA_CHROME_PATH,
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)
$BrowserExe = $edgeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $BrowserExe) {
    Write-Host "[FATAL] Edge/Chrome not found"
    exit 1
}

function Test-Cdp {
    try {
        $r = Invoke-WebRequest -Uri "$CdpUrl/json/version" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -ge 200
    } catch { return $false }
}

function Get-ActiveProfile {
    $pathFile = Join-Path $Root "run\captcha_profile_path.txt"
    if (Test-Path $pathFile) {
        $p = (Get-Content $pathFile -Raw).Trim()
        if ($p -and (Test-Path $p)) { return $p }
    }
    # default pool p0
    $p0 = Join-Path $Root "browser_data\edge_pool\p0"
    New-Item -ItemType Directory -Force -Path (Join-Path $p0 "Default") | Out-Null
    return $p0
}

function Start-Browser([string]$UserData) {
    $profile = if ($env:CAPTCHA_CHROME_PROFILE) { $env:CAPTCHA_CHROME_PROFILE } else { "Default" }
    New-Item -ItemType Directory -Force -Path (Join-Path $UserData $profile) | Out-Null
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] start browser data=$UserData"
    $args = @(
        "--remote-debugging-port=$Port",
        "--user-data-dir=$UserData",
        "--profile-directory=$profile",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
        "--start-maximized"
    )
    Start-Process -FilePath $BrowserExe -ArgumentList $args -WindowStyle Minimized | Out-Null
    for ($i = 1; $i -le 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-Cdp) { Write-Host "[ok] CDP ready"; return $true }
    }
    Write-Host "[warn] CDP not ready after start"
    return $false
}

function Stop-CdpBrowsers {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^(msedge|chrome)\.exe$' -and $_.CommandLine -and (
            $_.CommandLine -match "remote-debugging-port=$Port" -or
            $_.CommandLine -match 'edge_pool|chrome_env_v3|chrome_clean_manual'
        )
    } | ForEach-Object {
        cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
    }
    Start-Sleep -Seconds 1
}

$lastProfile = ""
Write-Host "pm2-browser-cdp loop root=$Root exe=$BrowserExe"
while ($true) {
    try {
        $profile = Get-ActiveProfile
        $cdpOk = Test-Cdp
        if (-not $cdpOk) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] CDP down -> start profile=$profile"
            Start-Browser $profile | Out-Null
            $lastProfile = $profile
        } elseif ($lastProfile -and ($profile -ne $lastProfile)) {
            # 资料已轮换：换进程挂新资料
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] profile changed $lastProfile -> $profile"
            Stop-CdpBrowsers
            Start-Browser $profile | Out-Null
            $lastProfile = $profile
        } elseif (-not $lastProfile) {
            $lastProfile = $profile
        }
    } catch {
        Write-Host "[err] $_"
    }
    Start-Sleep -Seconds $CheckSec
}

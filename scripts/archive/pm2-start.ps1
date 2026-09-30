# Start all xianyu services under PM2 (interactive session).
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs\pm2") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "run") | Out-Null

# seed profile pool p0..p3
for ($i = 0; $i -lt 4; $i++) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "browser_data\edge_pool\p$i\Default") | Out-Null
}
if (-not (Test-Path (Join-Path $Root "run\captcha_profile_index.txt"))) {
    Set-Content (Join-Path $Root "run\captcha_profile_index.txt") "0" -Encoding ASCII
}
$p0 = Join-Path $Root "browser_data\edge_pool\p0"
Set-Content (Join-Path $Root "run\captcha_profile_path.txt") $p0 -Encoding ASCII

# copy .env into services
foreach ($svc in @("backend-web", "websocket", "scheduler")) {
    $src = Join-Path $Root ".env"
    $dst = Join-Path $Root "$svc\.env"
    if (Test-Path $src) { Copy-Item $src $dst -Force }
}

# ensure MySQL/Redis
foreach ($s in @("MySQL", "Redis")) {
    $svc = Get-Service $s -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Running") {
        try { Start-Service $s; Write-Host "[infra] started $s" } catch { Write-Host "[infra] $s fail: $_" }
    } else {
        Write-Host "[infra] $s ok"
    }
}

# stop legacy supervisors / loops
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -match 'supervisor\.ps1' -or
        $_.CommandLine -match 'run-loop\.ps1' -or
        $_.CommandLine -match 'watchdog\.ps1'
    )
} | ForEach-Object {
    Write-Host "[cleanup] kill $($_.ProcessId)"
    cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
}

# kill port holders (apps only)
foreach ($p in 8089, 8090, 8091, 5173) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        cmd /c "taskkill /PID $($_.OwningProcess) /T /F >nul 2>&1"
    }
}

# pm2
$pm2 = (Get-Command pm2 -ErrorAction SilentlyContinue)
if (-not $pm2) {
    Write-Host "[FATAL] pm2 not found. Install: npm i -g pm2"
    exit 1
}

Write-Host "pm2 delete old apps (if any)..."
pm2 delete backend-web websocket scheduler frontend 2>$null | Out-Null
pm2 start (Join-Path $Root "ecosystem.config.cjs")
pm2 save 2>$null | Out-Null
pm2 status

Write-Host ""
Write-Host "waiting for ports..."
# 不含 9222：过滑块用的 Edge 由 Python 侧按需拉起，启动阶段本来就不该在监听。
for ($i = 1; $i -le 25; $i++) {
    Start-Sleep -Seconds 2
    $up = 0
    foreach ($p in 8089, 8090, 8091, 5173) {
        if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { $up++ }
    }
    Write-Host "  try $i : $up/4"
    if ($up -ge 4) { break }
}

foreach ($u in @(
    "http://127.0.0.1:8089/health",
    "http://127.0.0.1:8090/health",
    "http://127.0.0.1:8091/health",
    "http://127.0.0.1:5173/"
)) {
    try {
        $code = (Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 8).StatusCode
        Write-Host "OK $code $u"
    } catch {
        Write-Host "FAIL $u"
    }
}

Write-Host ""
Write-Host "PM2: pm2 status | pm2 logs | pm2 restart all"
Write-Host "URLs: http://127.0.0.1:5173  :8089 :8090 :8091  CDP:9222"

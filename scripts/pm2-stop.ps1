# Stop PM2 apps for xianyu-auto-reply
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
pm2 delete backend-web websocket scheduler frontend browser-cdp 2>$null
pm2 save 2>$null
# optional: leave browser; kill cdp browsers from pool
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(msedge|chrome)\.exe$' -and $_.CommandLine -and (
        $_.CommandLine -match 'remote-debugging-port=9222' -or
        $_.CommandLine -match 'edge_pool'
    )
} | ForEach-Object {
    cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
}
Write-Host "PM2 apps stopped."

[CmdletBinding()]
param([switch]$NoWait)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$sessionId = (Get-Process -Id $PID).SessionId
if ($sessionId -eq 0) {
    throw "This script must run in the logged-in desktop session, not Session 0."
}

$env:PM2_HOME = Join-Path $env:USERPROFILE ".pm2"
$pm2Command = Get-Command pm2.cmd -ErrorAction SilentlyContinue
if (-not $pm2Command) {
    $pm2Command = Get-Command pm2 -ErrorAction SilentlyContinue
}
if (-not $pm2Command) {
    throw "PM2 was not found. Install Node.js and PM2 first."
}
$pm2 = $pm2Command.Source

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs\pm2") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "run") | Out-Null
foreach ($i in 0..3) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "browser_data\edge_pool\p$i\Default") | Out-Null
}

$profileIndex = Join-Path $Root "run\captcha_profile_index.txt"
if (-not (Test-Path $profileIndex)) { Set-Content -LiteralPath $profileIndex -Value "0" -Encoding ASCII }
$profilePath = Join-Path $Root "run\captcha_profile_path.txt"
Set-Content -LiteralPath $profilePath -Value (Join-Path $Root "browser_data\edge_pool\p0") -Encoding ASCII

foreach ($serviceName in @("MySQL84", "Memurai")) {
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne "Running") {
        Start-Service -Name $serviceName
    }
    if (-not $service) { Write-Warning "Windows service not found: $serviceName" }
}

$rootEnv = Join-Path $Root ".env"
if (Test-Path $rootEnv) {
    foreach ($serviceName in @("backend-web", "websocket", "scheduler")) {
        Copy-Item -LiteralPath $rootEnv -Destination (Join-Path $Root "$serviceName\.env") -Force
    }
}

& $pm2 start (Join-Path $Root "ecosystem.config.cjs") --update-env
if ($LASTEXITCODE -ne 0) { throw "PM2 start failed: exit code $LASTEXITCODE" }
& $pm2 save
if ($LASTEXITCODE -ne 0) { throw "PM2 save failed: exit code $LASTEXITCODE" }
& $pm2 status

if (-not $NoWait) {
    foreach ($port in @(8089, 8090, 8091, 5173)) {
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
                $ready = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { Write-Warning "Port $port is not listening" }
    }
}

Write-Host "Project started in user Session $sessionId. PM2_HOME=$env:PM2_HOME"

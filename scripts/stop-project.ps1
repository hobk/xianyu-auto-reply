[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$sessionId = (Get-Process -Id $PID).SessionId
if ($sessionId -eq 0) {
    throw "This script must run in the logged-in desktop session, not Session 0."
}

$env:PM2_HOME = Join-Path $env:USERPROFILE ".pm2"
$pm2Command = Get-Command pm2.cmd -ErrorAction SilentlyContinue
if (-not $pm2Command) { $pm2Command = Get-Command pm2 -ErrorAction SilentlyContinue }
if (-not $pm2Command) { throw "PM2 was not found." }
$pm2 = $pm2Command.Source

& $pm2 delete backend-web websocket scheduler frontend 2>$null
& $pm2 save
& $pm2 status
Write-Host "Project services stopped; other PM2 projects were not changed."

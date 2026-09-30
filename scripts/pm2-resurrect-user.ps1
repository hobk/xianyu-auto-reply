[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$env:PM2_HOME = Join-Path $env:USERPROFILE ".pm2"
$pm2Command = Get-Command pm2.cmd -ErrorAction SilentlyContinue
if (-not $pm2Command) { $pm2Command = Get-Command pm2 -ErrorAction SilentlyContinue }
if (-not $pm2Command) { throw "PM2 was not found." }
& $pm2Command.Source resurrect
if ($LASTEXITCODE -ne 0) { throw "PM2 resurrect failed." }
# Apply the current browser configuration rather than stale values in dump.pm2.
& $pm2Command.Source startOrRestart (Join-Path $Root "ecosystem.config.cjs") --only websocket --update-env
if ($LASTEXITCODE -ne 0) { throw "Websocket configuration refresh failed." }
& $pm2Command.Source save

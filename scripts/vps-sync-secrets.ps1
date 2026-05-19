# fandango_watcher → shared vps kit
param(
    [string]$ProjectName = "fandango-watcher"
)

$Root = Split-Path -Parent $PSScriptRoot
$env:VPS_PROJECT_ENV = Join-Path $Root "vps/projects/fandango-watcher.env"
$env:VPS_PROJECT_NAME = $ProjectName

& bash (Join-Path $Root "vps/scripts/sync-secrets.sh")

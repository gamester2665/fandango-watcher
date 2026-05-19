# Sync local secret files to VPS (never commit these files).
param(
    [string]$ProjectName = $env:VPS_PROJECT_NAME
)

$ErrorActionPreference = "Stop"
$Kit = Split-Path -Parent $PSScriptRoot
$Lib = Join-Path $Kit "projects"

if (-not $ProjectName) { $ProjectName = "fandango-watcher" }
$ProjectEnv = Join-Path $Lib "$ProjectName.env"
if (-not (Test-Path $ProjectEnv)) { throw "missing project env: $ProjectEnv" }

$env:VPS_PROJECT_ENV = $ProjectEnv
$env:VPS_PROJECT_NAME = $ProjectName

& bash (Join-Path $Kit "scripts/sync-secrets.sh")

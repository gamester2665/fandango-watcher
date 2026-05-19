# Sync local secrets + config to VPS (never commit these files).
param(
    [string]$VpsHost = $env:FANDANGO_VPS_HOST,
    [string]$VpsUser = $env:FANDANGO_VPS_SSH_USER,
    [string]$RemoteDir = $env:FANDANGO_VPS_DIR
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if (-not $VpsHost) { $VpsHost = "74.48.91.123" }
if (-not $VpsUser) { $VpsUser = "root" }
if (-not $RemoteDir) { $RemoteDir = "/root/fandango-watcher" }

$envFile = Join-Path $Root ".env"
$configFile = Join-Path $Root "config.yaml"
if (-not (Test-Path $envFile)) { throw "missing .env at repo root" }
if (-not (Test-Path $configFile)) { throw "missing config.yaml at repo root" }

$target = "${VpsUser}@${VpsHost}:${RemoteDir}/"
Write-Host "Uploading to $target (creates .env.production on server)"

scp $envFile "${VpsUser}@${VpsHost}:${RemoteDir}/.env.production"
scp $configFile "${VpsUser}@${VpsHost}:${RemoteDir}/config.yaml"

ssh "${VpsUser}@${VpsHost}" "chmod 600 ${RemoteDir}/.env.production ${RemoteDir}/config.yaml && sed -i 's/\r$//' ${RemoteDir}/.env.production 2>/dev/null || true"
Write-Host "Sync OK. Deploy with: bash scripts/vps-deploy.sh"

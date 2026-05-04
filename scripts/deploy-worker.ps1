# Deploy the Cloudflare Python Worker (wrangler.toml + pywrangler).
# Prereqs: Node.js (for npx wrangler), uv, and Cloudflare auth (wrangler login
# or CLOUDFLARE_API_TOKEN).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

npx --yes wrangler versions list *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Wrangler is not authenticated. Run ``npx wrangler login`` or set CLOUDFLARE_API_TOKEN in .env (see .env.example). If OAuth returns 400: ``npx wrangler logout`` then login again." -ForegroundColor Red
  exit 1
}

uv sync --group dev
uv run pywrangler deploy @args

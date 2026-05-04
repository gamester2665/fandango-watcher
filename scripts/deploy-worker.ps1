# Deploy the Cloudflare Python Worker (wrangler.toml + pywrangler).
# Prereqs: Node.js (for npx wrangler), uv, and Cloudflare auth (wrangler login
# or CLOUDFLARE_API_TOKEN).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
uv sync --group dev
uv run pywrangler deploy @args

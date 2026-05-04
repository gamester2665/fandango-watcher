# Deploy the Cloudflare Python Worker (wrangler.toml + pywrangler).
# Prereqs: Node.js (for npx wrangler), uv, and Cloudflare auth (wrangler login
# or CLOUDFLARE_API_TOKEN).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$envFile = Join-Path (Get-Location) ".env"
if (-not $env:CLOUDFLARE_API_TOKEN -and (Test-Path -LiteralPath $envFile)) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        if ($_ -match '^\s*CLOUDFLARE_API_TOKEN\s*=\s*(.+)\s*$') {
            $raw = $Matches[1].Trim()
            if (($raw.StartsWith('"') -and $raw.EndsWith('"')) -or ($raw.StartsWith("'") -and $raw.EndsWith("'"))) {
                $raw = $raw.Substring(1, $raw.Length - 2)
            }
            if ($raw) { $env:CLOUDFLARE_API_TOKEN = $raw }
        }
    }
}

npx --yes wrangler versions list *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Wrangler is not authenticated. Run ``npx wrangler login`` or set CLOUDFLARE_API_TOKEN in .env (see .env.example). If OAuth returns 400: ``npx wrangler logout`` then login again." -ForegroundColor Red
  exit 1
}

uv sync --group dev
uv run pywrangler deploy @args

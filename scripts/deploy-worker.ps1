# Deploy the Cloudflare Python Worker.
# On Windows, prefer: bash scripts/deploy-worker-gha.sh  (GitHub Actions — no local python storm)
# Or: bash scripts/deploy-worker.sh  (uvx pywrangler; may hang on pyodide — use GHA if so)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Read-CloudflareTokenFromFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $last = $null
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*CLOUDFLARE_API_TOKEN\s*=\s*(.+)\s*$') {
            $raw = $Matches[1].Trim()
            if (($raw.StartsWith('"') -and $raw.EndsWith('"')) -or ($raw.StartsWith("'") -and $raw.EndsWith("'"))) {
                $raw = $raw.Substring(1, $raw.Length - 2)
            }
            if ($raw) { $last = $raw }
        }
    }
    if ($last) {
        $env:CLOUDFLARE_API_TOKEN = $last
        return $true
    }
    return $false
}

if (-not $env:CLOUDFLARE_API_TOKEN -and $env:FANDANGO_WATCHER_ENV_FILE) {
    [void](Read-CloudflareTokenFromFile -Path $env:FANDANGO_WATCHER_ENV_FILE)
}
if (-not $env:CLOUDFLARE_API_TOKEN) {
    foreach ($rel in @(".env.local", ".env")) {
        $p = Join-Path (Get-Location) $rel
        if (Read-CloudflareTokenFromFile -Path $p) { break }
    }
}
if (-not $env:CLOUDFLARE_API_TOKEN -and $env:CF_API_TOKEN) {
    $env:CLOUDFLARE_API_TOKEN = $env:CF_API_TOKEN
}

npx --yes wrangler whoami *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Wrangler is not authenticated. Run ``npx wrangler login`` or set CLOUDFLARE_API_TOKEN (environment, .env / .env.local, or FANDANGO_WATCHER_ENV_FILE). CF_API_TOKEN is accepted as an alias. For GitHub, add secret CLOUDFLARE_API_TOKEN and run workflow Deploy Cloudflare Worker. If OAuth returns 400: ``npx wrangler logout`` then login again." -ForegroundColor Red
  exit 1
}

$env:UV_CONCURRENT_BUILDS = if ($env:UV_CONCURRENT_BUILDS) { $env:UV_CONCURRENT_BUILDS } else { "1" }
$env:UV_CONCURRENT_DOWNLOADS = if ($env:UV_CONCURRENT_DOWNLOADS) { $env:UV_CONCURRENT_DOWNLOADS } else { "2" }
$env:UV_CONCURRENT_INSTALLS = if ($env:UV_CONCURRENT_INSTALLS) { $env:UV_CONCURRENT_INSTALLS } else { "1" }
uvx --from workers-py pywrangler deploy @args

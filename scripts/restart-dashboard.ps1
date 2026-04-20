# Stop whatever is LISTENING on the dashboard port, then start the UI again.
# From repo root:  powershell -ExecutionPolicy Bypass -File scripts/restart-dashboard.ps1
# Stops the old process first so `uv sync` does not fail with "fandango-watcher.exe in use".
$ErrorActionPreference = "Continue"
$port = if ($env:WATCHER_HEALTHZ_PORT) { $env:WATCHER_HEALTHZ_PORT } else { "8787" }
netstat -ano | Select-String ":$port\s.*LISTENING" | ForEach-Object {
    $pid = ($_.ToString() -split '\s+')[-1]
    if ($pid -match '^\d+$') {
        & taskkill /F /PID $pid 2>$null
    }
}
Start-Sleep -Seconds 1
Set-Location (Join-Path $PSScriptRoot "..")
& uv run fandango-watcher dashboard @args

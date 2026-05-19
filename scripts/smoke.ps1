$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "== doctor =="
uv run fandango-watcher doctor

Write-Host "== pytest =="
uv run pytest -q

Write-Host "== api-drift =="
uv run fandango-watcher api-drift --max-dates 3

Write-Host "== x bearer =="
uv run fandango-watcher x-poll --check-bearer

if ($env:SMOKE_NOTIFY -eq "1") {
    Write-Host "== test-notify =="
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    uv run fandango-watcher test-notify `
        --subject "fandango-watcher smoke" `
        --body "Smoke test OK ($ts)"
}

Write-Host "Smoke OK. Start watch: uv run fandango-watcher watch --no-open"

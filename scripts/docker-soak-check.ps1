# Quick local Docker soak sanity check (see docs/docker_implementation.md Phase 5).
param(
    [int]$MinTicks = 2,
    [int]$MaxErrorTicks = 0
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/docker-common.ps1"
Ensure-RepoRoot
Ensure-Docker

Write-Host "== compose ps =="
docker compose ps

Write-Host "== healthz =="
$raw = curl.exe -fsS http://127.0.0.1:8787/healthz
$hb = $raw | ConvertFrom-Json

Write-Host ($raw)

if ($hb.status -ne "ok") { throw "healthz status not ok" }
if ($hb.total_ticks -lt $MinTicks) {
    throw "total_ticks=$($hb.total_ticks) expected >= $MinTicks (watch loop may be stuck)"
}
if ($hb.total_errors -gt $MaxErrorTicks) {
    throw "total_errors=$($hb.total_errors) exceeds max $MaxErrorTicks"
}

Write-Host "== recent logs =="
docker compose logs watcher --tail 15

Write-Host "Soak check OK (ticks=$($hb.total_ticks) errors=$($hb.total_errors))."

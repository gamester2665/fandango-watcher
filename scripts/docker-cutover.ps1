# Cutover from host uv watch to Docker Compose watcher (see docs/docker_implementation.md).
param(
    [switch]$SkipBuild,
    [switch]$SkipSeed,
    [switch]$SeedOnly,
    [switch]$NoProfileSeed,
    [switch]$NotifySmoke,
    [switch]$Rollback
)

. "$PSScriptRoot/docker-common.ps1"
Ensure-RepoRoot
Ensure-Docker

if ($Rollback) {
    docker compose down
    Write-Host "Docker watcher stopped."
    Write-Host "Restart host watcher:"
    Write-Host "  uv run fandango-watcher watch --config config.yaml --no-open"
    exit 0
}

try { Save-DockerBaseline } catch { }

if (Test-Port8787InUse) {
    Write-Error @"
port 8787 is in use — stop host uv watch before cutover
  netstat -ano | findstr :8787
  taskkill /F /PID <pid>
If baseline was just saved, stop the host watcher and re-run with -SkipBuild -SkipSeed when appropriate.
"@
}

& "$PSScriptRoot/docker-volume-backup.ps1" -All

if (-not $SkipBuild) {
    Write-Host "== build =="
    docker compose build watcher
    if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }
}

if (-not $SkipSeed) {
    $seedArgs = @("-State")
    if (-not $NoProfileSeed) {
        $seedArgs += "-Profile"
    }
    & "$PSScriptRoot/docker-seed-volumes.ps1" @seedArgs
}

if ($SeedOnly) {
    Write-Host "seed-only complete"
    exit 0
}

$smokeArgs = @()
if ($NotifySmoke -or $env:SMOKE_NOTIFY -eq "1") {
    $smokeArgs += "-NotifySmoke"
}
& "$PSScriptRoot/docker-smoke.ps1" @smokeArgs

Write-Host ""
Write-Host "Cutover complete."
Write-Host "  Dashboard: http://127.0.0.1:8787/"
Write-Host "  Status:    curl.exe -fsS http://127.0.0.1:8787/api/status"
Write-Host "  Logs:      docker compose logs -f watcher"
Write-Host "  Rollback:  scripts/docker-cutover.ps1 -Rollback"

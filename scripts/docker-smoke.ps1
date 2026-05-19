# Docker runtime smoke: doctor, api-drift, x bearer, start watcher, healthz.
param(
    [switch]$NotifySmoke
)

. "$PSScriptRoot/docker-common.ps1"
Ensure-RepoRoot
Ensure-Docker

if ($NotifySmoke -or $env:SMOKE_NOTIFY -eq "1") {
    $script:DoNotify = $true
} else {
    $script:DoNotify = $false
}

function Invoke-Check {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$Block
    )
    Write-Host "== $Label =="
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$Label failed" }
}

Invoke-Check "doctor" {
    docker compose run --rm watcher doctor
}
Invoke-Check "api-drift" {
    docker compose run --rm watcher api-drift --max-dates 3
}
Invoke-Check "x-bearer" {
    docker compose run --rm watcher x-poll --check-bearer
}

if (Test-Port8787InUse) {
    Write-Host "port 8787 already in use; assuming watcher is running"
} else {
    Invoke-Check "up" { docker compose up -d watcher }
}

Invoke-Check "healthz" { Wait-Healthz }

if ($script:DoNotify) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Invoke-Check "test-notify" {
        docker compose run --rm watcher test-notify `
            --subject "fandango-watcher docker smoke" `
            --body "Docker smoke OK ($ts)"
    }
}

Write-Host "Docker smoke OK."
Write-Host "Dashboard: http://127.0.0.1:8787/"
Write-Host "Logs: docker compose logs -f watcher"

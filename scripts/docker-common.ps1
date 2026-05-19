# Shared helpers for Docker operator scripts (see docs/docker_implementation.md).
$ErrorActionPreference = "Stop"

$script:DockerRepoRoot = Split-Path -Parent $PSScriptRoot

function Ensure-RepoRoot {
    Set-Location $script:DockerRepoRoot
    foreach ($required in @("Dockerfile", "docker-compose.yml", "pyproject.toml")) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:DockerRepoRoot $required))) {
            throw "Run from fandango_watcher repo root; missing $required"
        }
    }
}

function Ensure-Docker {
    docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose is not available"
    }
}

function Ensure-VolumesExist {
    docker compose up --no-start watcher | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "failed to ensure compose volumes exist" }
}

function Get-ComposeVolumeName {
    param([Parameter(Mandatory)][string]$Key)

    Ensure-VolumesExist
    $found = docker volume ls -q --filter "label=com.docker.compose.volume=$Key" 2>$null | Select-Object -First 1
    if (-not $found) {
        $found = docker volume ls -q 2>$null | Where-Object { $_ -match "_${Key}$" } | Select-Object -First 1
    }
    if (-not $found) {
        throw "could not resolve Docker volume for compose key $Key"
    }
    return $found
}

function Test-VolumeNonempty {
    param([Parameter(Mandatory)][string]$Volume)
    $out = docker run --rm -v "${Volume}:/v:ro" alpine sh -c "ls -A /v 2>/dev/null | head -1"
    return [bool]$out
}

function Copy-HostDirToVolume {
    param(
        [Parameter(Mandatory)][string]$HostDir,
        [Parameter(Mandatory)][string]$Volume
    )
    if (-not (Test-Path -LiteralPath $HostDir)) {
        Write-Host "skip seed: host directory missing: $HostDir"
        return
    }
    $hostPath = (Resolve-Path -LiteralPath $HostDir).Path
    docker run --rm `
        -v "${Volume}:/dest" `
        -v "${hostPath}:/src:ro" `
        alpine sh -c "mkdir -p /dest && cp -a /src/. /dest/"
    if ($LASTEXITCODE -ne 0) { throw "seed failed for $HostDir" }
    Write-Host "seeded $HostDir -> $Volume"
}

function Test-Port8787InUse {
    $lines = netstat -ano 2>$null | Select-String ":8787"
    return [bool]$lines
}

function Save-DockerBaseline {
    $dir = Join-Path $script:DockerRepoRoot "artifacts/docker-baseline"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $healthz = Join-Path $dir "healthz-before.json"
    $status = Join-Path $dir "status-before.json"
    & curl.exe -fsS "http://127.0.0.1:8787/healthz" -o $healthz 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "saved healthz-before.json" }
    & curl.exe -fsS "http://127.0.0.1:8787/api/status" -o $status 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "saved status-before.json" }
}

function Wait-Healthz {
    param([int]$MaxAttempts = 15)
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        curl.exe -fsS http://127.0.0.1:8787/healthz | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 2
    }
    throw "healthz not ready after ${MaxAttempts} attempts"
}

function Get-VolumeAlias {
    param([Parameter(Mandatory)][string]$Key)
    switch ($Key) {
        "fandango_state" { return "state" }
        "fandango_profile" { return "profile" }
        "fandango_artifacts" { return "artifacts" }
        default { throw "unknown volume key: $Key" }
    }
}

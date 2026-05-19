# Backup named Docker Compose volumes to backups/docker-volumes/.
param(
    [switch]$All,
    [string]$Volumes = ""
)

. "$PSScriptRoot/docker-common.ps1"
Ensure-RepoRoot
Ensure-Docker

$BackupDir = Join-Path $DockerRepoRoot "backups/docker-volumes"
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$Keys = @()

function Map-AliasToKey {
    param([string]$Alias)
    switch ($Alias.Trim()) {
        "state" { return "fandango_state" }
        "profile" { return "fandango_profile" }
        "artifacts" { return "fandango_artifacts" }
        default { throw "unknown volume alias: $Alias" }
    }
}

if ($All -or [string]::IsNullOrWhiteSpace($Volumes)) {
    $Keys = @("fandango_state", "fandango_profile", "fandango_artifacts")
} else {
    foreach ($part in $Volumes.Split(",")) {
        $Keys += Map-AliasToKey $part
    }
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

foreach ($key in $Keys) {
    $vol = Get-ComposeVolumeName $key
    $fileName = "${key}_${Stamp}.tar.gz"
    docker run --rm `
        -v "${vol}:/volume:ro" `
        -v "${BackupDir}:/backup" `
        alpine sh -c "tar -czf /backup/$fileName -C /volume ."
    if ($LASTEXITCODE -ne 0) { throw "backup failed for $key" }
    Write-Host "backup: $(Join-Path $BackupDir $fileName)"
}

Write-Host @"

Restore example:
  docker run --rm -v <volume>:/volume -v "`$PWD/backups/docker-volumes:/backup:ro" alpine sh -c 'rm -rf /volume/* && tar -xzf /backup/<file>.tar.gz -C /volume'
"@

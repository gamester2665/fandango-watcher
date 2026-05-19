# Seed host state/profile/artifacts into named Docker Compose volumes.
param(
    [switch]$State,
    [switch]$Profile,
    [switch]$Artifacts,
    [switch]$All,
    [switch]$Force
)

. "$PSScriptRoot/docker-common.ps1"
Ensure-RepoRoot
Ensure-Docker

if (-not ($State -or $Profile -or $Artifacts -or $All)) {
    $State = $true
}
if ($All) {
    $State = $true
    $Profile = $true
    $Artifacts = $true
}

function Maybe-BackupAndClear {
    param([Parameter(Mandatory)][string]$Key)
    $vol = Get-ComposeVolumeName $Key
    if (Test-VolumeNonempty $vol) {
        if (-not $Force) {
            throw "volume $vol is non-empty; pass -Force to overwrite (backup runs first)"
        }
        $alias = Get-VolumeAlias $Key
        & "$PSScriptRoot/docker-volume-backup.ps1" -Volumes $alias
        docker run --rm -v "${vol}:/v" alpine sh -c "rm -rf /v/* /v/.[!.]* /v/..?* 2>/dev/null || true"
    }
}

function Seed-One {
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$HostDir
    )
    Maybe-BackupAndClear $Key
    Copy-HostDirToVolume -HostDir $HostDir -Volume (Get-ComposeVolumeName $Key)
}

if ($State) {
    Seed-One "fandango_state" (Join-Path $DockerRepoRoot "state")
}
if ($Profile) {
    Seed-One "fandango_profile" (Join-Path $DockerRepoRoot "browser-profile")
}
if ($Artifacts) {
    Seed-One "fandango_artifacts" (Join-Path $DockerRepoRoot "artifacts")
}

Write-Host "seed complete"

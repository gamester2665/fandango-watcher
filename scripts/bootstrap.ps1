$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Example = Join-Path $Root "config.example.yaml"
$Dest = Join-Path $Root "config.yaml"
if (Test-Path -LiteralPath $Dest) {
    Write-Host "config.yaml already exists: $Dest"
    exit 0
}
if (-not (Test-Path -LiteralPath $Example)) {
    Write-Error "missing $Example"
    exit 1
}
Copy-Item -LiteralPath $Example -Destination $Dest
Write-Host "Created $Dest from $Example"

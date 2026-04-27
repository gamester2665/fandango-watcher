# Merge docker-compose.yml + docker-compose.dev.yml — see docs/DOCKER_DEV.md
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ComposeArgs
)

if (-not $env:COMPOSE_FILE) {
    $env:COMPOSE_FILE = "docker-compose.yml:docker-compose.dev.yml"
}

if ($null -eq $ComposeArgs -or $ComposeArgs.Count -eq 0) {
    docker compose
} else {
    docker compose @ComposeArgs
}

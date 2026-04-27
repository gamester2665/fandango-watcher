#!/usr/bin/env bash
# Merge docker-compose.yml + docker-compose.dev.yml — see docs/DOCKER_DEV.md
set -euo pipefail
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:docker-compose.dev.yml}"
exec docker compose "$@"

#!/usr/bin/env bash
# Shared helpers for Docker operator scripts (see docs/docker_implementation.md).
set -euo pipefail

DOCKER_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ensure_repo_root() {
  cd "$DOCKER_REPO_ROOT"
  for required in Dockerfile docker-compose.yml pyproject.toml; do
    [[ -e "$required" ]] || {
      echo "missing $required; run from fandango_watcher repo root" >&2
      exit 1
    }
  done
}

ensure_docker() {
  docker compose version >/dev/null 2>&1 || {
    echo "docker compose is not available" >&2
    exit 1
  }
}

ensure_volumes_exist() {
  docker compose up --no-start watcher >/dev/null 2>&1
}

volume_name() {
  local key="$1"
  local found
  found="$(docker volume ls -q --filter "label=com.docker.compose.volume=${key}" 2>/dev/null | head -1 || true)"
  if [[ -z "$found" ]]; then
    ensure_volumes_exist
    found="$(docker volume ls -q --filter "label=com.docker.compose.volume=${key}" 2>/dev/null | head -1 || true)"
  fi
  if [[ -z "$found" ]]; then
    found="$(docker volume ls -q 2>/dev/null | grep "_${key}$" | head -1 || true)"
  fi
  if [[ -z "$found" ]]; then
    echo "could not resolve Docker volume for compose key ${key}" >&2
    exit 1
  fi
  echo "$found"
}

volume_nonempty() {
  local vol="$1"
  docker run --rm -v "${vol}:/v:ro" alpine sh -c 'ls -A /v 2>/dev/null | head -1' | grep -q .
}

seed_host_dir() {
  local host_dir="$1"
  local vol="$2"
  if [[ ! -d "$host_dir" ]]; then
    echo "skip seed: host directory missing: $host_dir"
    return 0
  fi
  docker run --rm \
    -v "${vol}:/dest" \
    -v "${host_dir}:/src:ro" \
    alpine sh -c 'mkdir -p /dest && cp -a /src/. /dest/'
  echo "seeded ${host_dir} -> ${vol}"
}

port_8787_in_use() {
  if command -v netstat >/dev/null 2>&1; then
    netstat -ano 2>/dev/null | grep -q ':8787.*LISTENING' && return 0
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ':8787' && return 0
  fi
  return 1
}

capture_baseline() {
  local dir="$DOCKER_REPO_ROOT/artifacts/docker-baseline"
  mkdir -p "$dir"
  if curl -fsS "http://127.0.0.1:8787/healthz" -o "$dir/healthz-before.json" 2>/dev/null; then
    echo "saved $dir/healthz-before.json"
  fi
  if curl -fsS "http://127.0.0.1:8787/api/status" -o "$dir/status-before.json" 2>/dev/null; then
    echo "saved $dir/status-before.json"
  fi
}

wait_for_healthz() {
  local max="${1:-15}"
  local i
  for ((i = 1; i <= max; i++)); do
    if curl -fsS http://127.0.0.1:8787/healthz >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "healthz not ready after ${max} attempts" >&2
  return 1
}

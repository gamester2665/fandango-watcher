#!/usr/bin/env bash
# Shared VPS kit helpers — source from other vps/scripts/*.sh scripts.
set -euo pipefail

vps_kit_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

vps_load_host_env() {
  local kit
  kit="$(vps_kit_root)"
  if [[ -f "$kit/host.env" ]]; then
    # shellcheck disable=SC1091
    source "$kit/host.env"
  fi
}

vps_resolve_project_env() {
  local kit repo_name candidate
  kit="$(vps_kit_root)"

  if [[ -n "${VPS_PROJECT_ENV:-}" && -f "$VPS_PROJECT_ENV" ]]; then
    echo "$VPS_PROJECT_ENV"
    return 0
  fi

  if [[ -n "${VPS_PROJECT_NAME:-}" && -f "$kit/projects/${VPS_PROJECT_NAME}.env" ]]; then
    echo "$kit/projects/${VPS_PROJECT_NAME}.env"
    return 0
  fi

  repo_name="$(basename "$(git -C "${VPS_REPO_ROOT:-$PWD}" rev-parse --show-toplevel 2>/dev/null || echo "$PWD")")"
  for candidate in \
    "$kit/projects/${repo_name}.env" \
    "$kit/projects/$(echo "$repo_name" | tr '_' '-').env" \
    "$kit/projects/$(echo "$repo_name" | tr '-' '_').env"; do
    if [[ -f "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done

  echo "could not resolve project env — set VPS_PROJECT_ENV or add vps/projects/<name>.env" >&2
  return 1
}

vps_load_project_env() {
  local project_env
  project_env="$(vps_resolve_project_env)"
  # shellcheck disable=SC1090
  source "$project_env"
  export VPS_PROJECT_ENV="$project_env"
}

vps_load_env() {
  vps_load_host_env
  vps_load_project_env

  export VPS_HOST="${VPS_HOST:-${FANDANGO_VPS_HOST:-${ROSE_VPS_HOST:-74.48.91.123}}}"
  export VPS_SSH_USER="${VPS_SSH_USER:-${FANDANGO_VPS_SSH_USER:-${ROSE_VPS_SSH_USER:-root}}}"
  export VPS_REMOTE_DIR="${VPS_REMOTE_DIR:-${FANDANGO_VPS_DIR:-}}"
  export VPS_REPO_ROOT="${VPS_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

  export ROSE_PROD_URL="${ROSE_PROD_URL:-https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z}"
  export VPS_MIN_FREE_GIB="${VPS_MIN_FREE_GIB:-10}"
  export VPS_MAIL_UNITS="${VPS_MAIL_UNITS:-postfix dovecot nginx mariadb}"
  export VPS_RESERVED_PORTS="${VPS_RESERVED_PORTS:-7166 8989 8080 3306 25 587}"
  export VPS_BRANCH="${VPS_BRANCH:-${DEPLOY_BRANCH:-main}}"
  export VPS_REMOTE="${VPS_REMOTE:-${DEPLOY_REMOTE:-origin}}"
  export VPS_HEALTHZ_PORT="${VPS_HEALTHZ_PORT:-8787}"
  export VPS_HEALTHZ_PATH="${VPS_HEALTHZ_PATH:-/healthz}"
  export VPS_COMPOSE_SERVICE="${VPS_COMPOSE_SERVICE:-app}"

  if [[ -z "${VPS_REMOTE_DIR:-}" ]]; then
    echo "VPS_REMOTE_DIR is required in project env" >&2
    return 1
  fi
}

vps_compose_args() {
  local args=()
  local file
  for file in ${VPS_COMPOSE_FILES:-docker-compose.yml}; do
    args+=(-f "$file")
  done
  printf '%s\n' "${args[@]}"
}

vps_compose_cmd() {
  local -a compose_args
  mapfile -t compose_args < <(vps_compose_args)
  docker compose "${compose_args[@]}" "$@"
}

vps_link_env_production() {
  if [[ -f .env.production ]]; then
    sed -i 's/\r$//' .env.production 2>/dev/null || true
    chmod 600 .env.production
    ln -sf .env.production .env
  fi
}

vps_secret_pairs() {
  # Prints "local:remote" one per line from VPS_SECRET_FILES.
  local pair local remote
  for pair in ${VPS_SECRET_FILES:-}; do
    local="${pair%%:*}"
    remote="${pair#*:}"
    if [[ -z "$local" || -z "$remote" || "$local" == "$remote" && "$pair" != *:* ]]; then
      echo "invalid VPS_SECRET_FILES entry: $pair" >&2
      return 1
    fi
    echo "${local}:${remote}"
  done
}

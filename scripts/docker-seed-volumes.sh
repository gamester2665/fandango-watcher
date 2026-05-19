#!/usr/bin/env bash
# Seed host state/profile/artifacts into named Docker Compose volumes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=docker-common.sh
source "$ROOT/scripts/docker-common.sh"

ensure_repo_root
ensure_docker

SEED_STATE=0
SEED_PROFILE=0
SEED_ARTIFACTS=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/docker-seed-volumes.sh [--state] [--profile] [--artifacts] [--all] [--force]

Default (no flags): seed state only.
--force overwrites non-empty destination volumes (runs backup first).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state) SEED_STATE=1; shift ;;
    --profile) SEED_PROFILE=1; shift ;;
    --artifacts) SEED_ARTIFACTS=1; shift ;;
    --all)
      SEED_STATE=1
      SEED_PROFILE=1
      SEED_ARTIFACTS=1
      shift
      ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$SEED_STATE$SEED_PROFILE$SEED_ARTIFACTS" == "000" ]]; then
  SEED_STATE=1
fi

volume_alias() {
  case "$1" in
    fandango_state) echo state ;;
    fandango_profile) echo profile ;;
    fandango_artifacts) echo artifacts ;;
    *) echo "unknown volume key: $1" >&2; exit 1 ;;
  esac
}

maybe_backup_and_clear() {
  local key="$1"
  local vol
  vol="$(volume_name "$key")"
  if volume_nonempty "$vol"; then
    if [[ "$FORCE" -ne 1 ]]; then
      echo "volume ${vol} is non-empty; pass --force to overwrite (backup runs first)" >&2
      exit 1
    fi
    bash "$DOCKER_REPO_ROOT/scripts/docker-volume-backup.sh" --volumes "$(volume_alias "$key")"
    docker run --rm -v "${vol}:/v" alpine sh -c 'rm -rf /v/* /v/.[!.]* /v/..?* 2>/dev/null || true'
  fi
}

seed_one() {
  local key="$1"
  local host_dir="$2"
  maybe_backup_and_clear "$key"
  seed_host_dir "$host_dir" "$(volume_name "$key")"
}

[[ "$SEED_STATE" -eq 1 ]] && seed_one fandango_state "$DOCKER_REPO_ROOT/state"
[[ "$SEED_PROFILE" -eq 1 ]] && seed_one fandango_profile "$DOCKER_REPO_ROOT/browser-profile"
[[ "$SEED_ARTIFACTS" -eq 1 ]] && seed_one fandango_artifacts "$DOCKER_REPO_ROOT/artifacts"

echo "seed complete"

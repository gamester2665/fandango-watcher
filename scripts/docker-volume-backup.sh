#!/usr/bin/env bash
# Backup named Docker Compose volumes to backups/docker-volumes/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=docker-common.sh
source "$ROOT/scripts/docker-common.sh"

ensure_repo_root
ensure_docker

BACKUP_DIR="$DOCKER_REPO_ROOT/backups/docker-volumes"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
VOLUMES=()

usage() {
  cat <<'EOF'
Usage: scripts/docker-volume-backup.sh [--all | --volumes state,profile,artifacts]

Writes tar.gz backups under backups/docker-volumes/.
EOF
}

map_key() {
  case "$1" in
    state) echo fandango_state ;;
    profile) echo fandango_profile ;;
    artifacts) echo fandango_artifacts ;;
    *) echo "unknown volume alias: $1" >&2; exit 1 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      VOLUMES=(fandango_state fandango_profile fandango_artifacts)
      shift
      ;;
    --volumes)
      shift
      IFS=',' read -r -a parts <<< "${1:-}"
      for part in "${parts[@]}"; do
        VOLUMES+=("$(map_key "$part")")
      done
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ${#VOLUMES[@]} -eq 0 ]]; then
  VOLUMES=(fandango_state fandango_profile fandango_artifacts)
fi

mkdir -p "$BACKUP_DIR"

for key in "${VOLUMES[@]}"; do
  vol="$(volume_name "$key")"
  out="$BACKUP_DIR/${key}_${STAMP}.tar.gz"
  docker run --rm \
    -v "${vol}:/volume:ro" \
    -v "${BACKUP_DIR}:/backup" \
    alpine sh -c "tar -czf /backup/$(basename "$out") -C /volume ."
  echo "backup: $out"
done

cat <<EOF

Restore example:
  docker run --rm -v <volume>:/volume -v "\$PWD/backups/docker-volumes:/backup:ro" \\
    alpine sh -c 'rm -rf /volume/* && tar -xzf /backup/<file>.tar.gz -C /volume'
EOF

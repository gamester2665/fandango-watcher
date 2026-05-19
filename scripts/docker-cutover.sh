#!/usr/bin/env bash
# Cutover from host uv watch to Docker Compose watcher (see docs/docker_implementation.md).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=docker-common.sh
source "$ROOT/scripts/docker-common.sh"

ensure_repo_root
ensure_docker

SKIP_BUILD=0
SKIP_SEED=0
SEED_ONLY=0
NO_PROFILE_SEED=0
NOTIFY_SMOKE=0
ROLLBACK=0

usage() {
  cat <<'EOF'
Usage: scripts/docker-cutover.sh [options]

Options:
  --skip-build         Skip docker compose build
  --skip-seed          Skip volume seed
  --seed-only          Seed volumes then exit
  --no-profile-seed    Do not seed browser-profile (state still seeded unless --skip-seed)
  --notify-smoke       Pass --notify-smoke to docker-smoke (or set SMOKE_NOTIFY=1)
  --rollback           Stop compose and print host uv restart command
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    --skip-seed) SKIP_SEED=1; shift ;;
    --seed-only) SEED_ONLY=1; shift ;;
    --no-profile-seed) NO_PROFILE_SEED=1; shift ;;
    --notify-smoke) NOTIFY_SMOKE=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$ROLLBACK" -eq 1 ]]; then
  docker compose down
  echo "Docker watcher stopped."
  echo "Restart host watcher:"
  echo "  uv run fandango-watcher watch --config config.yaml --no-open"
  exit 0
fi

if port_8787_in_use; then
  echo "warning: port 8787 is in use — stop host uv watch before cutover" >&2
  echo "  netstat -ano | findstr :8787   (Windows)" >&2
  echo "  lsof -nP -iTCP:8787 -sTCP:LISTEN   (POSIX)" >&2
  echo "If baseline was just saved, stop the host watcher and re-run with --skip-build --skip-seed when appropriate." >&2
  exit 1
fi

capture_baseline || true

bash "$DOCKER_REPO_ROOT/scripts/docker-volume-backup.sh" --all

if [[ "$SKIP_BUILD" -ne 1 ]]; then
  echo "== build =="
  docker compose build watcher
fi

if [[ "$SKIP_SEED" -ne 1 ]]; then
  seed_args=(--state)
  if [[ "$NO_PROFILE_SEED" -ne 1 ]]; then
    seed_args+=(--profile)
  fi
  bash "$DOCKER_REPO_ROOT/scripts/docker-seed-volumes.sh" "${seed_args[@]}"
fi

if [[ "$SEED_ONLY" -eq 1 ]]; then
  echo "seed-only complete"
  exit 0
fi

smoke_args=()
[[ "$NOTIFY_SMOKE" -eq 1 || "${SMOKE_NOTIFY:-}" == "1" ]] && smoke_args+=(--notify-smoke)
bash "$DOCKER_REPO_ROOT/scripts/docker-smoke.sh" "${smoke_args[@]}"

echo ""
echo "Cutover complete."
echo "  Dashboard: http://127.0.0.1:8787/"
echo "  Status:    curl -fsS http://127.0.0.1:8787/api/status"
echo "  Logs:      docker compose logs -f watcher"
echo "  Rollback:  scripts/docker-cutover.sh --rollback"

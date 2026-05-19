#!/usr/bin/env bash
# Docker runtime smoke: doctor, api-drift, x bearer, start watcher, healthz.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=docker-common.sh
source "$ROOT/scripts/docker-common.sh"

ensure_repo_root
ensure_docker

NOTIFY_SMOKE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --notify-smoke) NOTIFY_SMOKE=1; shift ;;
    -h|--help)
      echo "Usage: scripts/docker-smoke.sh [--notify-smoke]"
      echo "SMS test requires --notify-smoke or SMOKE_NOTIFY=1"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${SMOKE_NOTIFY:-}" == "1" ]]; then
  NOTIFY_SMOKE=1
fi

run_check() {
  echo "== $1 =="
  shift
  "$@"
}

run_check doctor docker compose run --rm watcher doctor
run_check api-drift docker compose run --rm watcher api-drift --max-dates 3
run_check x-bearer docker compose run --rm watcher x-poll --check-bearer

if port_8787_in_use; then
  echo "port 8787 already in use; assuming watcher is running"
else
  run_check up docker compose up -d watcher
fi

run_check healthz wait_for_healthz

if [[ "$NOTIFY_SMOKE" -eq 1 ]]; then
  run_check test-notify docker compose run --rm watcher test-notify \
    --subject "fandango-watcher docker smoke" \
    --body "Docker smoke OK ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
fi

echo "Docker smoke OK."
echo "Dashboard: http://127.0.0.1:8787/"
echo "Logs: docker compose logs -f watcher"

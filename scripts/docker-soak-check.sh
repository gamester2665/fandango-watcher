#!/usr/bin/env bash
# Quick local Docker soak sanity check (see docs/docker_implementation.md Phase 5).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=docker-common.sh
source "$ROOT/scripts/docker-common.sh"

MIN_TICKS="${1:-2}"
MAX_ERRORS="${2:-0}"

ensure_repo_root
ensure_docker

echo "== compose ps =="
docker compose ps

echo "== healthz =="
raw="$(curl -fsS http://127.0.0.1:8787/healthz)"
echo "$raw"

ticks="$(python -c "import json,sys; print(json.load(sys.stdin)['total_ticks'])" <<<"$raw")"
errors="$(python -c "import json,sys; print(json.load(sys.stdin)['total_errors'])" <<<"$raw")"
status="$(python -c "import json,sys; print(json.load(sys.stdin)['status'])" <<<"$raw")"

[[ "$status" == "ok" ]] || { echo "healthz status not ok" >&2; exit 1; }
(( ticks >= MIN_TICKS )) || {
  echo "total_ticks=$ticks expected >= $MIN_TICKS (watch loop may be stuck)" >&2
  exit 1
}
(( errors <= MAX_ERRORS )) || {
  echo "total_errors=$errors exceeds max $MAX_ERRORS" >&2
  exit 1
}

echo "== recent logs =="
docker compose logs watcher --tail 15

echo "Soak check OK (ticks=$ticks errors=$errors)."

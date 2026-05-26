#!/usr/bin/env bash
# On VPS: pull latest branch and restart the Docker stack.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$KIT/scripts/lib.sh"
vps_load_env

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

git fetch "$VPS_REMOTE" "$VPS_BRANCH"
git checkout "$VPS_BRANCH"
git merge --ff-only "$VPS_REMOTE/$VPS_BRANCH"

vps_link_env_production

bash "$KIT/scripts/preflight.sh"

vps_compose_cmd up -d --build "$VPS_COMPOSE_SERVICE"

vps_compose_cmd ps
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}" >/dev/null; then
    curl -fsS "http://127.0.0.1:${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}"
    echo ""
    break
  fi
  sleep 2
done || {
  echo "healthz not ready yet; check compose logs ${VPS_COMPOSE_SERVICE}" >&2
  exit 1
}

bash "$KIT/scripts/verify-neighbors.sh"

echo "VPS deploy OK — ${VPS_PROJECT_NAME}"

#!/usr/bin/env bash
# On VPS: pull latest main and restart the watcher stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${DEPLOY_BRANCH:-main}"
REMOTE="${DEPLOY_REMOTE:-origin}"

git fetch "$REMOTE" "$BRANCH"
git checkout "$BRANCH"
git merge --ff-only "$REMOTE/$BRANCH"

if [[ -f .env.production ]]; then
  sed -i 's/\r$//' .env.production
  chmod 600 .env.production
fi

docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build watcher

docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
curl -fsS http://127.0.0.1:8787/healthz || {
  echo "healthz not ready yet; check: docker compose logs watcher" >&2
  exit 1
}

echo "VPS deploy OK"

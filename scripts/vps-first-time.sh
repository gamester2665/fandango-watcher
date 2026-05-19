#!/usr/bin/env bash
# Run once on the VPS after cloning (manual SSH session). Does not upload secrets.
set -euo pipefail

REPO_URL="${FANDANGO_REPO_URL:-https://github.com/gamester2665/fandango-watcher.git}"
INSTALL_DIR="${FANDANGO_VPS_DIR:-/root/fandango-watcher}"

echo "== preflight =="
command -v docker >/dev/null
docker compose version >/dev/null
df -h /
docker system df || true

echo "== avoid Rose port collisions =="
ss -tlnp 2>/dev/null | grep -E ':7166|:8989|:8787' || true

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "== clone =="
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
git fetch origin main
git checkout main
git merge --ff-only origin/main

if [[ ! -f .env.production ]]; then
  echo "missing .env.production — copy from laptop (scripts/vps-sync-secrets.sh)" >&2
  exit 1
fi
if [[ ! -f config.yaml ]]; then
  echo "missing config.yaml — copy from laptop" >&2
  exit 1
fi

chmod 600 .env.production config.yaml
sed -i 's/\r$//' .env.production 2>/dev/null || true

echo "== prune build cache (shared 2.4GiB host) =="
docker builder prune -f >/dev/null 2>&1 || true

echo "== compose up =="
bash scripts/vps-pull-and-restart.sh

echo ""
echo "OK: curl -fsS http://127.0.0.1:8787/healthz"
echo "Rose check: curl -fsS -o /dev/null -w '%{http_code}\n' 'https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z'"

#!/usr/bin/env bash
# Run once on the VPS after cloning (manual SSH session). Does not upload secrets.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$KIT/scripts/lib.sh"
vps_load_env

INSTALL_DIR="${VPS_REMOTE_DIR}"

echo "== preflight =="
command -v docker >/dev/null
docker compose version >/dev/null
bash "$KIT/scripts/preflight.sh"
df -h /
docker system df || true

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "== clone =="
  git clone "$VPS_REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
git fetch origin "$VPS_BRANCH"
git checkout "$VPS_BRANCH"
git merge --ff-only "origin/$VPS_BRANCH"

missing=0
while IFS=: read -r _local remote; do
  [[ -f "$remote" ]] || {
    echo "missing $remote — copy from laptop (vps/scripts/sync-secrets.sh)" >&2
    missing=1
  }
done < <(vps_secret_pairs)
[[ "$missing" -eq 0 ]] || exit 1

vps_link_env_production
for pair in ${VPS_SECRET_FILES:-}; do
  remote="${pair#*:}"
  [[ -f "$remote" ]] && chmod 600 "$remote"
done

echo "== prune build cache (shared 2.4GiB host) =="
docker builder prune -f >/dev/null 2>&1 || true

echo "== compose up =="
bash "$KIT/scripts/pull-and-restart.sh"

echo ""
echo "OK: curl -fsS http://127.0.0.1:${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}"

#!/usr/bin/env bash
# From laptop: SSH to VPS and run pull-and-restart (does not upload secrets).
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$KIT/scripts/lib.sh"
vps_load_env

echo "Target: ${VPS_SSH_USER}@${VPS_HOST}:${VPS_REMOTE_DIR}"
echo "Project: ${VPS_PROJECT_NAME}"
echo "This script runs remote commands only; it does not upload secrets."
echo ""

ssh "${VPS_SSH_USER}@${VPS_HOST}" bash -s <<EOF
set -euo pipefail
export VPS_PROJECT_ENV='${VPS_PROJECT_ENV}'
export VPS_PROJECT_NAME='${VPS_PROJECT_NAME}'
cd "${VPS_REMOTE_DIR}"

if [[ ! -f docker-compose.yml ]]; then
  echo "missing ${VPS_REMOTE_DIR}/docker-compose.yml — clone the repo first" >&2
  exit 1
fi

docker builder prune -f >/dev/null 2>&1 || true
bash vps/scripts/pull-and-restart.sh
EOF

echo ""
echo "Verify from laptop (Tunnel hostname or SSH port-forward):"
echo "  ssh -L ${VPS_HEALTHZ_PORT}:127.0.0.1:${VPS_HEALTHZ_PORT} ${VPS_SSH_USER}@${VPS_HOST}"
echo "  curl -fsS http://127.0.0.1:${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}"

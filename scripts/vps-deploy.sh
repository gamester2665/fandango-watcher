#!/usr/bin/env bash
# First-time VPS bootstrap helper (run on laptop via SSH, or paste on server).
# Requires: repo cloned to /root/fandango-watcher, .env.production + config.yaml on server.
set -euo pipefail

VPS_HOST="${FANDANGO_VPS_HOST:-74.48.91.123}"
VPS_USER="${FANDANGO_VPS_SSH_USER:-root}"
REMOTE_DIR="${FANDANGO_VPS_DIR:-/root/fandango-watcher}"

echo "Target: ${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}"
echo "This script runs remote commands only; it does not upload secrets."
echo ""

ssh "${VPS_USER}@${VPS_HOST}" bash -s <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"

if [[ ! -f docker-compose.yml ]]; then
  echo "missing ${REMOTE_DIR}/docker-compose.yml — clone the repo first" >&2
  exit 1
fi
if [[ ! -f .env.production ]]; then
  echo "missing .env.production — copy from laptop (never commit)" >&2
  exit 1
fi
if [[ ! -f config.yaml ]]; then
  echo "missing config.yaml — copy from laptop" >&2
  exit 1
fi

chmod 600 .env.production
sed -i 's/\r$//' .env.production 2>/dev/null || true

docker builder prune -f >/dev/null 2>&1 || true
bash scripts/vps-pull-and-restart.sh
EOF

echo ""
echo "Verify from laptop (Tunnel hostname or SSH port-forward):"
echo "  curl -fsS http://127.0.0.1:8787/healthz   # on VPS via SSH -L 8787:127.0.0.1:8787"

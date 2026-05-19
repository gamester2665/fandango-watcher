#!/usr/bin/env bash
# Sync local .env + config.yaml to VPS (never commit these files).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_HOST="${FANDANGO_VPS_HOST:-74.48.91.123}"
VPS_USER="${FANDANGO_VPS_SSH_USER:-root}"
REMOTE_DIR="${FANDANGO_VPS_DIR:-/root/fandango-watcher}"

[[ -f "$ROOT/.env" ]] || { echo "missing $ROOT/.env" >&2; exit 1; }
[[ -f "$ROOT/config.yaml" ]] || { echo "missing $ROOT/config.yaml" >&2; exit 1; }

echo "Uploading to ${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

scp "$ROOT/.env" "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/.env.production"
scp "$ROOT/config.yaml" "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/config.yaml"

ssh "${VPS_USER}@${VPS_HOST}" "chmod 600 ${REMOTE_DIR}/.env.production ${REMOTE_DIR}/config.yaml && sed -i 's/\r$//' ${REMOTE_DIR}/.env.production 2>/dev/null || true"

echo "Sync OK. Deploy with: bash scripts/vps-deploy.sh"

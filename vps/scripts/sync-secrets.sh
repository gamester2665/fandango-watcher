#!/usr/bin/env bash
# Sync local secret files to VPS (never commit these files).
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$KIT/scripts/lib.sh"
vps_load_env

REPO_ROOT="${VPS_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "Uploading ${VPS_PROJECT_NAME} secrets to ${VPS_SSH_USER}@${VPS_HOST}:${VPS_REMOTE_DIR}/"

remote_cmds=()
while IFS=: read -r local remote; do
  local_path="$REPO_ROOT/$local"
  [[ -f "$local_path" ]] || { echo "missing $local_path" >&2; exit 1; }
  scp "$local_path" "${VPS_SSH_USER}@${VPS_HOST}:${VPS_REMOTE_DIR}/${remote}"
  remote_cmds+=("chmod 600 ${VPS_REMOTE_DIR}/${remote}")
done < <(vps_secret_pairs)

remote_cmds+=("ln -sf .env.production ${VPS_REMOTE_DIR}/.env 2>/dev/null || true")
remote_cmds+=("sed -i 's/\\r$//' ${VPS_REMOTE_DIR}/.env.production 2>/dev/null || true")

ssh "${VPS_SSH_USER}@${VPS_HOST}" "$(IFS='; '; echo "${remote_cmds[*]}")"

echo "Sync OK. Deploy with: bash vps/scripts/deploy-remote.sh"

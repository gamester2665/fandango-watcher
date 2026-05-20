#!/usr/bin/env bash
# Sync local secret files to VPS (never commit these files).
#
# Prefer vps/run_vps_cmd.py (paramiko + password from vps/host.env or secrets file)
# so Windows/Git Bash does not pop an SSH key passphrase dialog for scp/ssh.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="${VPS_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

if [[ "${VPS_SYNC_USE_SCP:-}" != "1" ]]; then
  project="${VPS_PROJECT_NAME:-}"
  if [[ -z "$project" ]]; then
    # shellcheck source=scripts/lib.sh
    source "$KIT/scripts/lib.sh"
    vps_load_project_env 2>/dev/null || true
    project="${VPS_PROJECT_NAME:-fandango-watcher}"
  fi
  exec python "$KIT/run_vps_cmd.py" --project "$project" --sync-secrets
fi

# shellcheck source=scripts/lib.sh
source "$KIT/scripts/lib.sh"
vps_load_env

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

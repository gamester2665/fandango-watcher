#!/usr/bin/env bash
# Publish public HTTPS via Cloudflare Tunnel API (reads vps/projects/*.env + .env token).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
vps_load_env

HOSTNAME="${VPS_PUBLIC_HOSTNAME:-}"
SERVICE="http://127.0.0.1:${VPS_HEALTHZ_PORT}"
STRATEGY="${VPS_TUNNEL_STRATEGY:-reuse}"
TUNNEL_ID="${VPS_TUNNEL_ID:-7050a8bf-2e17-4b87-a74d-277ab6b9ffb3}"

if [[ -z "$HOSTNAME" ]]; then
  echo "Set VPS_PUBLIC_HOSTNAME in project env (vps/projects/<name>.env)" >&2
  exit 1
fi

REPO_ROOT="$(git -C "${VPS_REPO_ROOT:-$PWD}" rev-parse --show-toplevel 2>/dev/null || pwd)"
exec python "$ROOT/scripts/cloudflare-publish-hostname.py" \
  --strategy "$STRATEGY" \
  --tunnel-id "$TUNNEL_ID" \
  --hostname "$HOSTNAME" \
  --service "$SERVICE" \
  "$@"

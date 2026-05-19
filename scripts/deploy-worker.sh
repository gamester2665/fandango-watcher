#!/usr/bin/env bash
# Deploy the Cloudflare Python Worker (wrangler.toml + pywrangler).
# Prereqs: Node.js (for npx wrangler), `uv`, and Cloudflare auth (`wrangler login`
# in an interactive shell, or CLOUDFLARE_API_TOKEN for CI/agents).
set -euo pipefail
cd "$(dirname "$0")/.."

_read_cloudflare_token_from_file() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local token
  token="$(
    grep -E '^[[:space:]]*CLOUDFLARE_API_TOKEN=' "$file" \
      | tail -n1 \
      | cut -d= -f2- \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" \
      | tr -d '\r' || true
  )"
  [[ -n "${token:-}" ]] || return 1
  export CLOUDFLARE_API_TOKEN="$token"
}

# Load CLOUDFLARE_API_TOKEN when unset (do not source whole env files).
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -n "${FANDANGO_WATCHER_ENV_FILE:-}" ]]; then
  _read_cloudflare_token_from_file "$FANDANGO_WATCHER_ENV_FILE" || true
fi
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  for envf in ".env.local" ".env"; do
    if _read_cloudflare_token_from_file "$envf"; then
      break
    fi
  done
fi
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -n "${CF_API_TOKEN:-}" ]]; then
  export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN"
fi

export CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-7f3e024b68ea359931e13d4688fde4a6}"

_wrangler_auth_ok() {
  if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    return 1
  fi
  # whoami fails for some scoped tokens even when deploy works; account_id is in wrangler.toml.
  if npx --yes wrangler whoami >/dev/null 2>&1; then
    return 0
  fi
  [[ -n "${CLOUDFLARE_API_TOKEN}" ]]
}

if ! _wrangler_auth_ok; then
  echo "Wrangler is not authenticated for API calls (needed for deploy)." >&2
  echo "  Fix: npx wrangler login   (local terminal)" >&2
  echo "  Fix: set CLOUDFLARE_API_TOKEN in the environment, or in .env / .env.local" >&2
  echo "       (see .env.example). Optional: FANDANGO_WATCHER_ENV_FILE=/path/to/.env" >&2
  echo "  Legacy: CF_API_TOKEN is accepted as an alias when CLOUDFLARE_API_TOKEN is unset." >&2
  echo "  CI/GitHub: add repository secret CLOUDFLARE_API_TOKEN and run workflow \"Deploy Cloudflare Worker\"." >&2
  echo "  If OAuth fails with 400: npx wrangler logout && npx wrangler login" >&2
  exit 1
fi

uv sync --group dev
exec uv run pywrangler deploy "$@"

#!/usr/bin/env bash
# Deploy the Cloudflare Python Worker (wrangler.toml + pywrangler).
# Prereqs: Node.js (for npx wrangler), `uv`, and Cloudflare auth (`wrangler login`
# in an interactive shell, or CLOUDFLARE_API_TOKEN for CI/agents).
set -euo pipefail
cd "$(dirname "$0")/.."

# Load CLOUDFLARE_API_TOKEN from .env when unset (do not source the whole file).
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -f .env ]]; then
  token="$(
    grep -E '^[[:space:]]*CLOUDFLARE_API_TOKEN=' .env \
      | tail -n1 \
      | cut -d= -f2- \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" \
      | tr -d '\r'
  )" || true
  if [[ -n "${token:-}" ]]; then
    export CLOUDFLARE_API_TOKEN="$token"
  fi
fi

if ! npx --yes wrangler versions list >/dev/null 2>&1; then
  echo "Wrangler is not authenticated for API calls (needed for deploy)." >&2
  echo "  Fix: npx wrangler login   (local terminal)" >&2
  echo "  Fix: add CLOUDFLARE_API_TOKEN to .env (agents/CI) — see .env.example" >&2
  echo "  If OAuth fails with 400: npx wrangler logout && npx wrangler login" >&2
  exit 1
fi

uv sync --group dev
exec uv run pywrangler deploy "$@"

#!/usr/bin/env bash
# Read-only neighbor safety checks before deploy — does not modify Rose/mail/tunnels.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
vps_load_env

fail() {
  echo "preflight FAILED: $*" >&2
  exit 1
}

warn() {
  echo "preflight WARN: $*" >&2
}

echo "== neighbor safety preflight (read-only) =="
echo "project: ${VPS_PROJECT_NAME:-unknown}  remote: ${VPS_REMOTE_DIR}"

if [[ "${PWD:-}" == *"rose-astrology"* ]]; then
  fail "current directory looks like rose-astrology; cd to ${VPS_REMOTE_DIR}"
fi

echo "-- mail stack (must stay active) --"
for unit in ${VPS_MAIL_UNITS}; do
  if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
    fail "$unit is not active — aborting; fix mail before deploy"
  fi
  echo "  $unit: active"
done

echo "-- Rose public health (must stay 200) --"
rose_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$ROSE_PROD_URL" || echo "000")"
if [[ "$rose_code" != "200" ]]; then
  fail "Rose returned HTTP $rose_code — aborting deploy until Rose is healthy"
fi
echo "  rose: HTTP $rose_code"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'rose-astrology'; then
  echo "  rose container: running"
else
  warn "rose-astrology container not seen in docker ps (tunnel may still work)"
fi

echo "-- reserved ports (existing services) --"
for port in ${VPS_RESERVED_PORTS}; do
  if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
    echo "  :${port} in use (expected — existing service)"
  fi
done

holder_pattern="${VPS_PORT_HOLDER_PATTERN:-docker}"
if ss -tlnp 2>/dev/null | grep ":${VPS_HEALTHZ_PORT} " | grep -vqE "$holder_pattern"; then
  if ss -tlnp 2>/dev/null | grep ":${VPS_HEALTHZ_PORT} " | grep -q '127.0.0.1'; then
    holder="$(ss -tlnp 2>/dev/null | grep ":${VPS_HEALTHZ_PORT} " || true)"
    if [[ -n "$holder" ]]; then
      fail "port ${VPS_HEALTHZ_PORT} already bound by another process: $holder"
    fi
  fi
fi
echo "  :${VPS_HEALTHZ_PORT} available for ${VPS_PROJECT_NAME} (127.0.0.1 bind)"

echo "-- disk (avoid starving mail/Rose during build) --"
free_gib="$(df -BG / | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
if [[ "${free_gib:-0}" -lt "$VPS_MIN_FREE_GIB" ]]; then
  warn "only ${free_gib}G free on / (recommend >= ${VPS_MIN_FREE_GIB}G); run docker builder prune -f"
else
  echo "  disk free: ${free_gib}G"
fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qi 'rose-astrology.*build'; then
  warn "Rose build may be in progress — wait before compose --build"
fi

echo "preflight OK — safe to deploy ${VPS_PROJECT_NAME} only (no Rose/mail/tunnel changes)"

#!/usr/bin/env bash
# Read-only checks before fandango_watcher deploy — does not modify Rose/mail/tunnels.
# See docs/VPS_COLOCATION_HANDOFF.md and docs/VPS_DEPLOY_PLAN.md § Safety.
set -euo pipefail

ROSE_URL="${ROSE_PROD_URL:-https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z}"
FANDANGO_PORT="${FANDANGO_HEALTHZ_PORT:-8787}"
MIN_FREE_GIB="${VPS_MIN_FREE_GIB:-10}"

fail() {
  echo "preflight FAILED: $*" >&2
  exit 1
}

warn() {
  echo "preflight WARN: $*" >&2
}

echo "== neighbor safety preflight (read-only) =="

# Never run from Rose's compose directory by mistake.
if [[ "${PWD:-}" == *"rose-astrology"* ]]; then
  fail "current directory looks like rose-astrology; cd to /root/fandango-watcher"
fi

echo "-- mail stack (must stay active) --"
for unit in postfix dovecot nginx mariadb; do
  if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
    fail "$unit is not active — aborting; fix mail before deploy"
  fi
  echo "  $unit: active"
done

echo "-- Rose public health (must stay 200) --"
rose_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$ROSE_URL" || echo "000")"
if [[ "$rose_code" != "200" ]]; then
  fail "Rose returned HTTP $rose_code — aborting deploy until Rose is healthy"
fi
echo "  rose: HTTP $rose_code"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'rose-astrology'; then
  echo "  rose container: running"
else
  warn "rose-astrology container not seen in docker ps (tunnel may still work)"
fi

echo "-- reserved ports (must not be taken by fandango) --"
for port in 7166 8989 8080 3306 25 587; do
  if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
    echo "  :${port} in use (expected — existing service)"
  fi
done

if ss -tlnp 2>/dev/null | grep ":${FANDANGO_PORT} " | grep -vq 'fandango'; then
  if ss -tlnp 2>/dev/null | grep ":${FANDANGO_PORT} " | grep -q '127.0.0.1'; then
    holder="$(ss -tlnp 2>/dev/null | grep ":${FANDANGO_PORT} " || true)"
    if [[ -n "$holder" ]] && ! echo "$holder" | grep -qiE 'fandango|docker.*watcher'; then
      fail "port ${FANDANGO_PORT} already bound by another process: $holder"
    fi
  fi
fi
echo "  :${FANDANGO_PORT} available for fandango_watcher (127.0.0.1 bind)"

echo "-- disk (avoid starving mail/Rose during build) --"
free_gib="$(df -BG / | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
if [[ "${free_gib:-0}" -lt "$MIN_FREE_GIB" ]]; then
  warn "only ${free_gib}G free on / (recommend >= ${MIN_FREE_GIB}G); run docker builder prune -f"
else
  echo "  disk free: ${free_gib}G"
fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qi 'rose-astrology.*build'; then
  warn "Rose build may be in progress — wait before fandango compose --build"
fi

echo "preflight OK — safe to deploy fandango_watcher only (no Rose/mail/tunnel changes)"

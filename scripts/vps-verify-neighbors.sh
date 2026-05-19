#!/usr/bin/env bash
# Post-deploy verification: fandango up AND Rose/mail still healthy.
set -euo pipefail

ROSE_URL="${ROSE_PROD_URL:-https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z}"
FANDANGO_PORT="${FANDANGO_HEALTHZ_PORT:-8787}"

fail() {
  echo "verify FAILED: $*" >&2
  exit 1
}

echo "== post-deploy neighbor verify =="

curl -fsS "http://127.0.0.1:${FANDANGO_PORT}/healthz" >/dev/null || fail "fandango healthz not responding"

rose_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$ROSE_URL" || echo "000")"
[[ "$rose_code" == "200" ]] || fail "Rose now HTTP $rose_code (was 200 pre-deploy)"

for unit in postfix dovecot nginx mariadb; do
  systemctl is-active --quiet "$unit" || fail "$unit no longer active"
done

echo "verify OK — fandango healthz + Rose + mail unchanged"

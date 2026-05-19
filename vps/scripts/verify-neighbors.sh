#!/usr/bin/env bash
# Post-deploy verification: project healthz AND Rose/mail still healthy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
vps_load_env

fail() {
  echo "verify FAILED: $*" >&2
  exit 1
}

echo "== post-deploy neighbor verify =="
echo "project: ${VPS_PROJECT_NAME:-unknown}"

curl -fsS "http://127.0.0.1:${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}" >/dev/null \
  || fail "${VPS_PROJECT_NAME} healthz not responding on :${VPS_HEALTHZ_PORT}${VPS_HEALTHZ_PATH}"

rose_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$ROSE_PROD_URL" || echo "000")"
[[ "$rose_code" == "200" ]] || fail "Rose now HTTP $rose_code (was 200 pre-deploy)"

for unit in ${VPS_MAIL_UNITS}; do
  systemctl is-active --quiet "$unit" || fail "$unit no longer active"
done

echo "verify OK — ${VPS_PROJECT_NAME} healthz + Rose + mail unchanged"

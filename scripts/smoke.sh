#!/usr/bin/env bash
# Local smoke: validate config/env, tests, API drift, and optional notify ping.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== doctor =="
uv run fandango-watcher doctor

echo "== pytest =="
uv run pytest -q

echo "== api-drift =="
uv run fandango-watcher api-drift --max-dates 3

echo "== x bearer =="
uv run fandango-watcher x-poll --check-bearer

if [[ "${SMOKE_NOTIFY:-}" == "1" ]]; then
  echo "== test-notify =="
  uv run fandango-watcher test-notify \
    --subject "fandango-watcher smoke" \
    --body "Smoke test OK ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
fi

echo "Smoke OK. Start watch: uv run fandango-watcher watch --no-open"

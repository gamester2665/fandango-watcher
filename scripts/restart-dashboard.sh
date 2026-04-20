#!/usr/bin/env bash
# Stop whatever is listening on the dashboard port, then start the UI again.
# Run from repo root (or any directory if uv finds the project).
set -euo pipefail
PORT="${WATCHER_HEALTHZ_PORT:-8787}"
PID="$(ss -lntp 2>/dev/null | awk -v p=":$PORT " '$0 ~ p {print}' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)"
if [[ -z "${PID}" ]] && command -v lsof >/dev/null 2>&1; then
  PID="$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
fi
if [[ -n "${PID}" ]]; then
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
fi
cd "$(dirname "$0")/.."
exec uv run fandango-watcher dashboard "$@"

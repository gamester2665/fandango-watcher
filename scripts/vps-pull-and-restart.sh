#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export VPS_PROJECT_ENV="$ROOT/vps/projects/fandango-watcher.env"
export VPS_PROJECT_NAME=fandango-watcher
exec bash "$ROOT/vps/scripts/pull-and-restart.sh" "$@"

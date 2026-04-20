#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$ROOT/config.example.yaml"
DEST="$ROOT/config.yaml"
if [[ -f "$DEST" ]]; then
  echo "config.yaml already exists: $DEST"
  exit 0
fi
if [[ ! -f "$EXAMPLE" ]]; then
  echo "error: missing $EXAMPLE" >&2
  exit 1
fi
cp "$EXAMPLE" "$DEST"
echo "Created $DEST from $EXAMPLE"

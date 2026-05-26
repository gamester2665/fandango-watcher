#!/usr/bin/env bash
# Paste into Hostinger / VPS web console when laptop SSH (port 22) is blocked.
# Pulls latest main, enables disclosed SMS in config.yaml, rebuilds watcher.
set -euo pipefail

cd /root/fandango-watcher

if [[ ! -f docker-compose.yml ]]; then
  echo "missing /root/fandango-watcher/docker-compose.yml" >&2
  exit 1
fi

if [[ -f config.yaml ]] && ! grep -q 'release_transition_showtimes_disclosed' config.yaml; then
  python3 - <<'PY'
from pathlib import Path

path = Path("config.yaml")
text = path.read_text(encoding="utf-8")
needle = "    - release_transition_bad_to_good"
event = "    - release_transition_showtimes_disclosed"
if event not in text and needle in text:
    text = text.replace(needle, event + "\n" + needle, 1)
    path.write_text(text, encoding="utf-8")
    print("patched config.yaml: added release_transition_showtimes_disclosed")
elif event in text:
    print("config.yaml already has release_transition_showtimes_disclosed")
else:
    raise SystemExit("could not patch config.yaml — add release_transition_showtimes_disclosed manually")
PY
fi

export VPS_PROJECT_ENV='/root/fandango-watcher/vps/projects/fandango-watcher.env'
export VPS_PROJECT_NAME='fandango-watcher'
docker builder prune -f >/dev/null 2>&1 || true
bash vps/scripts/pull-and-restart.sh

echo "deployed commit: $(git log -1 --oneline)"
curl -fsS http://127.0.0.1:8787/healthz
echo ""
grep -n 'release_transition' config.yaml | head -10 || true

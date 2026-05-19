#!/usr/bin/env python3
"""fandango_watcher wrapper — delegates to vps/run_vps_cmd.py."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("VPS_PROJECT_ENV", str(ROOT / "vps/projects/fandango-watcher.env"))
os.environ.setdefault("VPS_PROJECT_NAME", "fandango-watcher")

args = sys.argv[1:]
if not any(a in ("-p", "--project") for a in args):
    args = ["--project", "fandango-watcher", *args]

raise SystemExit(
    subprocess.call([sys.executable, str(ROOT / "vps/run_vps_cmd.py"), *args])
)

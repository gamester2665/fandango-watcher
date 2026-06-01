#!/usr/bin/env python3
"""Apply movie aspect-ratio schema migrations and seed research notes to D1."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_wrangler_file(sql_file: Path, *, remote: bool) -> None:
    flag = "--remote" if remote else "--local"
    full = f"npx wrangler d1 execute fandango_watcher_db {flag} --file {sql_file.as_posix()}"
    print(f"+ {full}")
    subprocess.run(full, cwd=ROOT, check=True, shell=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local",
        action="store_true",
        help="Apply to local D1 instead of remote.",
    )
    args = parser.parse_args()
    remote = not args.local

    sql_file = ROOT / "scripts" / "seed-movie-aspect-ratios.sql"
    _run_wrangler_file(sql_file, remote=remote)

    print("Done seeding movie aspect intel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

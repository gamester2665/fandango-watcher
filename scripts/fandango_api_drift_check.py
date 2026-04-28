"""Manual live drift check for the observed private Fandango API contract.

This script is intentionally opt-in and is not part of the default test suite.
It performs live network requests to Fandango, then prints a compact JSON report
with calendar coverage, date-level formats, showtime counts, buyable counts, and
format names seen in the inspected payloads.

Usage:
    ./.venv/Scripts/python.exe scripts/fandango_api_drift_check.py --max-dates 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fandango_watcher.fandango_api import FandangoApiClient, drift_check  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a live drift check against Fandango's observed JSON API."
    )
    parser.add_argument("--theater-id", default="AAAWX")
    parser.add_argument("--chain-code", default="AMC")
    parser.add_argument(
        "--max-dates",
        type=int,
        default=1,
        help="How many calendar dates to inspect with the showtimes endpoint.",
    )
    args = parser.parse_args(argv)

    with FandangoApiClient(
        theater_id=args.theater_id,
        chain_code=args.chain_code,
    ) as client:
        report = drift_check(client, max_dates=args.max_dates)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

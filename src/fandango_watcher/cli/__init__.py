"""Command-line interface.

Subcommands mirror the phased plan:

* ``once``         -- Phase 2: single crawl, print JSON, exit
* ``watch``        -- Phase 3: long poll loop with /healthz
* ``test-notify``  -- Phase 3: exercise SMS + email
* ``login``        -- Phase 5: headed first-run login (warms the persistent profile)
* ``test-purchase``-- Phase 4: plan + JSON; optional ``--stub`` runs scripted
                       checkout to the review page without clicking Complete
* ``refs``         -- print bundled development reference Fandango URLs (Schema A/B/C)
* ``doctor``       -- validate config + environment hints (notify creds, purchase mode)

``watch`` wires ``purchaser.run_scripted_purchase`` on release transitions
when ``purchase.mode`` allows. ``test-purchase`` plans only; add ``--stub``
for a live scripted run to the review page without clicking Complete.
"""

from __future__ import annotations

from collections.abc import Sequence

from .commands import (
    _run_api_drift,
    _run_dashboard,
    _run_doctor,
    _run_dump_review,
    _run_login,
    _run_movies,
    _run_once,
    _run_refs,
    _run_test_notify,
    _run_test_purchase,
    _run_watch,
    _run_x_poll,
)
from .logging_setup import configure_logging
from .parser import build_parser

STUB_EXIT_CODE = 2

__all__ = [
    "STUB_EXIT_CODE",
    "build_parser",
    "configure_logging",
    "main",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    if args.command == "once":
        return _run_once(args)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
    if args.command == "api-drift":
        return _run_api_drift(args)
    if args.command == "test-notify":
        return _run_test_notify(args)
    if args.command == "login":
        return _run_login(args)
    if args.command == "test-purchase":
        return _run_test_purchase(args)
    if args.command == "refs":
        return _run_refs(args)
    if args.command == "x-poll":
        return _run_x_poll(args)
    if args.command == "movies":
        return _run_movies(args)
    if args.command == "dump-review":
        return _run_dump_review(args)
    if args.command == "doctor":
        return _run_doctor(args)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":
    raise SystemExit(main())

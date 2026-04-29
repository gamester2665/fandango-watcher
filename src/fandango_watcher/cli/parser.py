"""Argparse definitions for the ``fandango_watcher`` CLI."""

from __future__ import annotations

import argparse
import os


def _register_format_filter_cli_args(p: argparse.ArgumentParser) -> None:
    """Shared ``--format-filter-*`` flags for commands that call ``crawl_target``."""
    p.add_argument(
        "--format-filter-selector",
        default=None,
        help=(
            "CSS selector for a Fandango format chip to click before extract "
            "(see config target format_filter_click_selector). Overrides YAML "
            "when set."
        ),
    )
    p.add_argument(
        "--format-filter-label",
        default=None,
        help=(
            "Format chip label substring, case-insensitive (e.g. 'IMAX 3D'). "
            "Used if --format-filter-selector is unset. Overrides YAML when set."
        ),
    )
    p.add_argument(
        "--format-filter-timeout-ms",
        type=int,
        default=None,
        metavar="MS",
        help="Timeout ms for the format-chip click (default: 12000). Overrides YAML.",
    )


def _register_direct_api_cli_args(p: argparse.ArgumentParser) -> None:
    """Shared direct API routing controls for commands that detect targets."""
    p.add_argument(
        "--direct-api-mode",
        choices=["auto", "api", "browser"],
        default="auto",
        help=(
            "Detection path override: auto uses config, api forces direct "
            "Fandango JSON API, browser forces Playwright/browser crawl."
        ),
    )
    p.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help=(
            "When direct API is enabled, fail on direct API errors instead of "
            "falling back to Playwright."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fandango-watcher",
        description=(
            "Dockerized Fandango watcher + A-List auto-purchaser for "
            "AMC Universal CityWalk Hollywood."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Root logger level (default: INFO).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- once ---------------------------------------------------------------
    p_once = subparsers.add_parser(
        "once",
        help="Run a single crawl and print the parsed JSON result.",
    )
    p_once.add_argument(
        "--config",
        default=None,
        help="Path to YAML config (default: $WATCHER_CONFIG or ./config.yaml).",
    )
    p_once.add_argument(
        "--target",
        default=None,
        help="Name of the target to crawl (default: first target in config).",
    )
    p_once.add_argument(
        "--url",
        default=None,
        help="Ad-hoc URL to crawl; bypasses config.targets when set.",
    )
    p_once.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Skip writing a PNG screenshot for this crawl.",
    )
    p_once.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl + classify + print JSON only (no side effects beyond screenshot).",
    )
    p_once.add_argument(
        "--headed",
        action="store_true",
        help="Force headed Chromium (overrides config.browser.headless).",
    )
    p_once.add_argument(
        "--video",
        action="store_true",
        help=(
            "Record a .webm of the crawl into ./artifacts/videos/ "
            "(or browser.record_video_dir)."
        ),
    )
    p_once.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Record a Playwright trace .zip you can open with "
            "`npx playwright show-trace <file>` (DOM + screenshots + network)."
        ),
    )
    p_once.add_argument(
        "--write-state",
        action="store_true",
        help=(
            "After a successful crawl, run the same transition()+save as "
            "`watch` and write state/<target>.json (requires --config; not "
            "compatible with --url). stdout JSON becomes "
            '{"parsed": ... , "state_write": ...}.'
        ),
    )
    _register_direct_api_cli_args(p_once)
    _register_format_filter_cli_args(p_once)

    # -- watch --------------------------------------------------------------
    p_watch = subparsers.add_parser(
        "watch",
        help="Run the long poll loop.",
    )
    p_watch.add_argument("--config", default=None)
    p_watch.add_argument(
        "--no-healthz",
        action="store_true",
        help="Skip the /healthz HTTP server (useful for local runs).",
    )
    p_watch.add_argument(
        "--healthz-port",
        type=int,
        default=None,
        help=(
            "Port to bind /healthz on. Default: 8787 or ``WATCHER_HEALTHZ_PORT`` from .env."
        ),
    )
    p_watch.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Exit after N ticks (useful for smoke tests).",
    )
    p_watch.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Force headed Chromium so you can watch the crawl + purchase flow "
            "in a real window. Overrides config.browser.headless."
        ),
    )
    p_watch.add_argument(
        "--video",
        action="store_true",
        help="Record a .webm per crawl/purchase context (artifacts/videos/).",
    )
    p_watch.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Record a Playwright trace .zip per crawl/purchase context. "
            "Open with `npx playwright show-trace <file>` for time-travel "
            "debugging (DOM + screenshots + network + console)."
        ),
    )
    p_watch.add_argument(
        "--no-open",
        action="store_true",
        help=(
            "Do not open the read-only dashboard in a browser on startup "
            "(the URL is still logged). Ignored if --no-healthz."
        ),
    )
    p_watch.add_argument(
        "--dashboard-refresh-seconds",
        type=int,
        default=10,
        metavar="N",
        help=(
            "HTML auto-refresh interval for the dashboard (meta refresh). "
            "Use 0 to disable. Default 10."
        ),
    )
    _register_direct_api_cli_args(p_watch)
    _register_format_filter_cli_args(p_watch)

    # -- dashboard ----------------------------------------------------------
    p_dash = subparsers.add_parser(
        "dashboard",
        help=(
            "Serve the read-only dashboard only (no Fandango crawl). "
            "Browse state + artifacts at http://127.0.0.1:8787/ by default."
        ),
    )
    p_dash.add_argument("--config", default=None)
    p_dash.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default 127.0.0.1).",
    )
    p_dash.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "TCP port. Default: 8787 or ``WATCHER_HEALTHZ_PORT`` from .env."
        ),
    )
    p_dash.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open a browser window.",
    )
    p_dash.add_argument(
        "--refresh-seconds",
        type=int,
        default=10,
        metavar="N",
        help=(
            "HTML auto-refresh interval (meta refresh). 0 disables. Default 10."
        ),
    )

    # -- api-drift ----------------------------------------------------------
    p_api = subparsers.add_parser(
        "api-drift",
        help="Run an opt-in live drift check against the direct Fandango API.",
    )
    p_api.add_argument("--config", default=None)
    p_api.add_argument(
        "--max-dates",
        type=int,
        default=3,
        metavar="N",
        help="Maximum theaterCalendar dates to inspect (default: 3).",
    )
    p_api.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Output format (default: text).",
    )

    # -- login --------------------------------------------------------------
    p_login = subparsers.add_parser(
        "login",
        help="Headed first-run login to warm the persistent Fandango profile.",
    )
    p_login.add_argument("--config", default=None)
    p_login.add_argument(
        "--login-url",
        default=None,
        help="Override the Fandango sign-in URL (default: official sign-in page).",
    )
    p_login.add_argument(
        "--headless",
        action="store_true",
        help="Force headless (debug only -- you will not see the login form).",
    )

    # -- test-notify --------------------------------------------------------
    p_test_notify = subparsers.add_parser(
        "test-notify",
        help="Exercise configured Twilio + SMTP channels with a test message.",
    )
    p_test_notify.add_argument("--config", default=None)
    p_test_notify.add_argument(
        "--subject",
        default="fandango_watcher test",
        help="Subject line of the test notification.",
    )
    p_test_notify.add_argument(
        "--body",
        default="This is a test notification from fandango_watcher.",
        help="Body text of the test notification.",
    )

    # -- test-purchase ------------------------------------------------------
    p_test_purchase = subparsers.add_parser(
        "test-purchase",
        help=(
            "Dry-run the purchase planner: crawl (or load a fixture), "
            "classify, plan, print JSON. Does NOT click anything."
        ),
    )
    p_test_purchase.add_argument("--config", default=None)
    p_test_purchase.add_argument(
        "--target",
        default=None,
        help="Name of the target to plan against (default: first target).",
    )
    p_test_purchase.add_argument(
        "--from-fixture",
        default=None,
        help=(
            "Path to a saved ParsedPageData JSON (output of `once`). "
            "Skips the live crawl entirely; useful for testing seat-priority "
            "rules deterministically."
        ),
    )
    p_test_purchase.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Skip the PNG screenshot for the live-crawl path.",
    )
    p_test_purchase.add_argument(
        "--stub",
        action="store_true",
        help=(
            "After a successful plan, run the scripted Playwright flow through "
            "seat selection and review, then stop before clicking "
            "'Complete Reservation' (hold_for_confirm). Requires "
            "purchase.enabled and a bookable plan; uses live Fandango."
        ),
    )
    p_test_purchase.add_argument(
        "--allow-stub-with-full-auto",
        action="store_true",
        help=(
            "Allow --stub when purchase.mode is full_auto (normally blocked so "
            "a production-style config is not used for manual checkout drills)."
        ),
    )
    _register_format_filter_cli_args(p_test_purchase)

    # -- x-poll -------------------------------------------------------------
    p_xpoll = subparsers.add_parser(
        "x-poll",
        help=(
            "Phase 2.5 — one-shot poll of configured X (Twitter) handles "
            "for early ticket-release hints. Prints matches as JSON. "
            "Advances state/social_x.json so subsequent calls are incremental."
        ),
    )
    p_xpoll.add_argument("--config", default=None)
    p_xpoll.add_argument(
        "--state-dir",
        default=None,
        help="Override config.state.dir (defaults to config value).",
    )
    p_xpoll.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Wipe state/social_x.json before polling so the next poll "
            "re-evaluates the latest tweets even if we've already seen them."
        ),
    )
    p_xpoll.add_argument(
        "--check-bearer",
        action="store_true",
        help=(
            "Validate X_BEARER_TOKEN by resolving the first configured "
            "handle (one cheap users/by/username call). Does NOT consume "
            "tweet quota or advance state. Exit 0 on success."
        ),
    )

    # -- dump-review --------------------------------------------------------
    p_dump = subparsers.add_parser(
        "dump-review",
        help=(
            "Capture a Fandango review-page DOM snapshot to a JSON fixture "
            "(plus a screenshot) so the invariant test corpus can grow from "
            "real $0.00 review pages. Pair with `tests/test_review_fixtures.py`."
        ),
    )
    p_dump.add_argument("--config", default=None)
    p_dump.add_argument(
        "--url",
        required=True,
        help="Fandango review-page URL to capture (must already be reachable).",
    )
    p_dump.add_argument(
        "--name",
        required=True,
        help=(
            "Fixture filename stem, e.g. 'odyssey_imax_70mm_alist_2026'. "
            "Output: tests/fixtures/review_pages/<name>.json + .png"
        ),
    )
    p_dump.add_argument(
        "--out-dir",
        default="tests/fixtures/review_pages",
        help="Directory to write fixtures into.",
    )
    p_dump.add_argument(
        "--wait-ms",
        type=int,
        default=4000,
        help="Extra dwell time after page load before snapshotting (default 4000).",
    )
    p_dump.add_argument(
        "--headed",
        action="store_true",
        help="Force headed Chromium (recommended — Fandango sometimes "
        "behaves differently headless).",
    )

    # -- movies -------------------------------------------------------------
    p_movies = subparsers.add_parser(
        "movies",
        help=(
            "Print the configured movie registry (movie -> Fandango targets "
            "+ X handles + keywords). Useful for verifying movie<->handle "
            "matching before flipping social_x.enabled. No network access."
        ),
    )
    p_movies.add_argument("--config", default=None)
    p_movies.add_argument(
        "--key",
        default=None,
        help="Print a single movie by key.",
    )
    p_movies.add_argument(
        "--output",
        choices=["json", "table"],
        default="table",
        help="Output format (default: table).",
    )

    # -- doctor -------------------------------------------------------------
    p_doctor = subparsers.add_parser(
        "doctor",
        help=(
            "Validate config load + env readiness (notify channels, purchase mode, "
            "X polling). Exits 1 if the config file is missing or invalid."
        ),
    )
    p_doctor.add_argument(
        "--config",
        default=None,
        help="Path to YAML config (default: $WATCHER_CONFIG or ./config.yaml).",
    )
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON to stdout.",
    )

    # -- refs ---------------------------------------------------------------
    p_refs = subparsers.add_parser(
        "refs",
        help=(
            "Print bundled development reference pages (URLs + expected schema). "
            "No network access."
        ),
    )
    p_refs.add_argument(
        "--key",
        default=None,
        help="Print a single reference entry by key (see REFERENCE_PAGE_KEYS).",
    )
    p_refs.add_argument(
        "--output",
        dest="output",
        choices=["json", "table"],
        default="json",
        help="Output format (default: json).",
    )

    return parser
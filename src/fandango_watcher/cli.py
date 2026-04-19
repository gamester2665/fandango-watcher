"""Command-line interface.

Subcommands mirror the phased plan:

* ``once``         -- Phase 2: single crawl, print JSON, exit
* ``watch``        -- Phase 3: long poll loop with /healthz
* ``test-notify``  -- Phase 3: exercise SMS + email
* ``login``        -- Phase 5: headed first-run login (warms the persistent profile)
* ``test-purchase``-- Phase 4: dry-run the purchase planner against a live URL or
                       a saved JSON fixture, prints the resulting ``PurchasePlan``
* ``refs``         -- print bundled development reference Fandango URLs (Schema A/B/C)

The end-to-end click flow (``purchaser.py``) is intentionally not yet wired —
``test-purchase`` validates the planner + invariant in isolation so we can
calibrate the seat-priority config without touching live checkout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import (
    BrowserConfig,
    NotifyConfig,
    Settings,
    TargetConfig,
    ViewportConfig,
    load_config,
)

logger = logging.getLogger("fandango_watcher")

STUB_EXIT_CODE = 2


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------


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
        default=8787,
        help="Port to bind /healthz on (default 8787).",
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
        default=8787,
        help="TCP port (default 8787).",
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


# -----------------------------------------------------------------------------
# Subcommand handlers
# -----------------------------------------------------------------------------


def _resolve_config_path(explicit: str | None) -> Path:
    candidate = explicit or os.environ.get("WATCHER_CONFIG") or "config.yaml"
    return Path(candidate)


def _apply_format_filter_cli_overrides(
    target: TargetConfig,
    args: argparse.Namespace,
) -> TargetConfig:
    """Merge ``--format-filter-*`` CLI overrides into a target (YAML or ad-hoc)."""
    sel = args.format_filter_selector
    lab = args.format_filter_label
    timeout = args.format_filter_timeout_ms
    if sel is None and lab is None and timeout is None:
        return target
    updates: dict[str, Any] = {}
    if sel is not None:
        updates["format_filter_click_selector"] = sel
    if lab is not None:
        updates["format_filter_click_label"] = lab
    if timeout is not None:
        updates["format_filter_click_timeout_ms"] = timeout
    return target.model_copy(update=updates)


def _run_once(args: argparse.Namespace) -> int:
    # Imported lazily so `argparse --help` and stub subcommands don't pay the
    # cost of starting up Playwright.
    from .watcher import crawl_target

    if args.write_state and args.url:
        print(
            "error: --write-state requires a config file (--config); "
            "it cannot be used with --url ad-hoc mode.",
            file=sys.stderr,
        )
        return 1

    cfg_for_state: Any = None
    if args.url:
        # Ad-hoc mode: synthesize a minimal target + browser config so the
        # user can `fandango-watcher once --url <URL>` without a config file.
        target = _apply_format_filter_cli_overrides(
            TargetConfig(name="adhoc", url=args.url),
            args,
        )
        browser_cfg = BrowserConfig(
            headless=not args.headed,
            user_data_dir="./browser-profile",
            viewport=ViewportConfig(),
            record_video=bool(args.video),
            record_trace=bool(args.trace),
        )
        citywalk_anchor = "AMC Universal CityWalk"
        screenshot_dir: Path | None = (
            None if args.no_screenshot else Path("./artifacts/screenshots")
        )
    else:
        config_path = _resolve_config_path(args.config)
        if not config_path.is_file():
            print(
                f"error: config file not found: {config_path}",
                file=sys.stderr,
            )
            return 1
        cfg = load_config(config_path)

        if args.target:
            matches = [t for t in cfg.targets if t.name == args.target]
            if not matches:
                print(
                    f"error: no target named {args.target!r} in {config_path}. "
                    f"available: {[t.name for t in cfg.targets]}",
                    file=sys.stderr,
                )
                return 1
            target = matches[0]
        else:
            target = cfg.targets[0]

        target = _apply_format_filter_cli_overrides(target, args)

        browser_cfg = cfg.browser
        overrides: dict[str, Any] = {}
        if args.headed:
            overrides["headless"] = False
        if args.video:
            overrides["record_video"] = True
        if args.trace:
            overrides["record_trace"] = True
        if overrides:
            browser_cfg = browser_cfg.model_copy(update=overrides)

        citywalk_anchor = cfg.theater.fandango_theater_anchor
        screenshot_dir = (
            None if args.no_screenshot else Path(cfg.screenshots.dir)
        )
        cfg_for_state = cfg

    logger.info(
        "crawling target=%s url=%s headless=%s",
        target.name,
        target.url,
        browser_cfg.headless,
    )

    result = crawl_target(
        target,
        browser_cfg=browser_cfg,
        citywalk_anchor=citywalk_anchor,
        screenshot_dir=screenshot_dir,
    )

    if args.write_state:
        from .state import load_target_state, save_target_state, transition

        assert cfg_for_state is not None
        state_dir = Path(cfg_for_state.state.dir)
        prev = load_target_state(state_dir, target.name)
        tr = transition(prev, result)
        written = save_target_state(state_dir, tr.state)
        out: dict[str, Any] = {
            "parsed": result.model_dump(mode="json"),
            "state_write": {
                "path": str(written),
                "events": tr.events,
                "target_state": tr.state.model_dump(mode="json"),
            },
        }
        json.dump(out, sys.stdout, indent=2, default=str)
    else:
        # ``mode="json"`` ensures datetimes and enums serialize cleanly.
        payload = result.model_dump(mode="json")
        json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def _run_watch(args: argparse.Namespace) -> int:
    import threading

    from .loop import install_signal_handlers, run_watch

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1

    cfg = load_config(config_path)
    settings = Settings()

    overrides: dict[str, Any] = {}
    if args.headed:
        overrides["headless"] = False
    if args.video:
        overrides["record_video"] = True
    if args.trace:
        overrides["record_trace"] = True
    if overrides:
        cfg = cfg.model_copy(update={"browser": cfg.browser.model_copy(update=overrides)})

    state_dir = Path(cfg.state.dir)
    screenshot_dir = Path(cfg.screenshots.dir)

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    healthz_port = None if args.no_healthz else args.healthz_port

    logger.info(
        "starting watch loop: targets=%d state_dir=%s healthz_port=%s",
        len(cfg.targets),
        state_dir,
        healthz_port,
    )

    return run_watch(
        cfg,
        settings,
        state_dir=state_dir,
        screenshot_dir=screenshot_dir,
        stop_event=stop_event,
        healthz_port=healthz_port,
        max_ticks=args.max_ticks,
        open_browser=not args.no_open,
        dashboard_refresh_seconds=args.dashboard_refresh_seconds,
    )


def _run_dashboard(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from .dashboard import DashboardData, DashboardPaths
    from .healthz import Heartbeat, start_healthz_server
    from .loop import install_signal_handlers

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1

    cfg = load_config(config_path)
    paths = DashboardPaths.from_config(cfg)
    hb = Heartbeat()
    settings = Settings()
    dd = DashboardData(
        cfg=cfg,
        paths=paths,
        heartbeat=hb,
        settings=settings,
        refresh_seconds=max(0, int(args.refresh_seconds)),
    )

    try:
        ctx = start_healthz_server(
            hb,
            host=args.host,
            port=args.port,
            dashboard_data=dd,
        )
    except OSError as e:
        print(
            f"error: failed to bind dashboard on {args.host}:{args.port}: {e}\n"
            f"hint: another dashboard process may still be holding the port.\n"
            f"  windows: netstat -ano | findstr :{args.port}\n"
            f"           taskkill /F /PID <pid>\n"
            f"  posix:   lsof -nP -iTCP:{args.port} -sTCP:LISTEN\n"
            f"           kill <pid>",
            file=sys.stderr,
        )
        return 1

    url = f"http://{args.host}:{ctx.port}/"
    logger.info("open dashboard: %s", url)
    if not args.no_open:
        try:
            webbrowser.open(url, new=0)
        except Exception:  # noqa: BLE001
            logger.debug("webbrowser.open failed", exc_info=True)

    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    stop_event.wait()
    try:
        ctx.stop()
    except Exception:  # noqa: BLE001
        logger.exception("error stopping dashboard server")
    return 0


def _run_test_notify(args: argparse.Namespace) -> int:
    from .notify import NotificationMessage, build_notifier

    config_path = _resolve_config_path(args.config)
    if config_path.is_file():
        notify_cfg = load_config(config_path).notify
    else:
        logger.warning(
            "no config found at %s; defaulting to both channels enabled",
            config_path,
        )
        notify_cfg = NotifyConfig()

    settings = Settings()
    notifier = build_notifier(notify_cfg, settings)

    if not notifier.channel_names:
        print(
            "error: no notification channels are configured. "
            "Set Twilio and/or SMTP env vars in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1

    msg = NotificationMessage(
        event="test_notify",
        subject=args.subject,
        body=args.body,
    )
    results = notifier.send(msg)

    had_failure = False
    for r in results:
        if r.ok:
            print(f"{r.name}: OK")
        else:
            print(f"{r.name}: FAIL -> {r.error!r}", file=sys.stderr)
            had_failure = True
    return 1 if had_failure else 0


def _run_login(args: argparse.Namespace) -> int:
    from .login import DEFAULT_LOGIN_URL, run_login

    config_path = _resolve_config_path(args.config)
    if config_path.is_file():
        browser_cfg = load_config(config_path).browser
    else:
        logger.warning(
            "no config found at %s; using defaults for browser settings",
            config_path,
        )
        browser_cfg = BrowserConfig(
            headless=False,
            user_data_dir="./browser-profile",
            viewport=ViewportConfig(),
        )

    headless_override: bool | None = True if args.headless else None

    return run_login(
        browser_cfg,
        login_url=args.login_url or DEFAULT_LOGIN_URL,
        headless_override=headless_override,
    )


def _run_test_purchase(args: argparse.Namespace) -> int:
    from .models import validate_page_data
    from .purchase import plan_purchase

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1
    cfg = load_config(config_path)

    if args.target:
        matches = [t for t in cfg.targets if t.name == args.target]
        if not matches:
            print(
                f"error: no target named {args.target!r}. "
                f"available: {[t.name for t in cfg.targets]}",
                file=sys.stderr,
            )
            return 1
        target = matches[0]
    else:
        target = cfg.targets[0]

    if args.from_fixture:
        fixture_path = Path(args.from_fixture)
        if not fixture_path.is_file():
            print(
                f"error: fixture file not found: {fixture_path}",
                file=sys.stderr,
            )
            return 1
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        parsed = validate_page_data(raw)
        logger.info(
            "loaded fixture %s release_schema=%s",
            fixture_path,
            parsed.release_schema,
        )
    else:
        from .watcher import crawl_target

        target = _apply_format_filter_cli_overrides(target, args)
        screenshot_dir = (
            None if args.no_screenshot else Path(cfg.screenshots.dir)
        )
        parsed = crawl_target(
            target,
            browser_cfg=cfg.browser,
            citywalk_anchor=cfg.theater.fandango_theater_anchor,
            screenshot_dir=screenshot_dir,
        )

    plan = plan_purchase(
        parsed,
        target_name=target.name,
        purchase_cfg=cfg.purchase,
    )

    if plan is None:
        print(
            json.dumps(
                {
                    "plan": None,
                    "reason": (
                        "no plan: purchase disabled, no CityWalk showtime, "
                        "or no matching seat-priority format"
                    ),
                    "release_schema": str(parsed.release_schema),
                },
                indent=2,
            )
        )
        return 0

    payload = {
        "plan": plan.model_dump(mode="json"),
        "release_schema": str(parsed.release_schema),
    }
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def _run_movies(args: argparse.Namespace) -> int:
    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1
    cfg = load_config(config_path)

    movies = cfg.movies
    if args.key:
        movies = [m for m in movies if m.key == args.key]
        if not movies:
            print(
                f"error: no movie with key {args.key!r}. "
                f"known: {[m.key for m in cfg.movies]}",
                file=sys.stderr,
            )
            return 1

    if args.output == "json":
        payload = [m.model_dump(mode="json") for m in movies]
        json.dump(payload if not args.key else payload[0], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not movies:
        print("(no movies configured)")
        return 0
    for m in movies:
        targets = ",".join(m.fandango_targets) or "-"
        handles = ",".join(f"@{h}" for h in m.x_handles) or "-"
        kw = ",".join(m.x_keywords) or "-"
        print(f"{m.key}\t{m.title}")
        print(f"  fandango_targets: {targets}")
        print(f"  x_handles:        {handles}")
        print(f"  x_keywords:       {kw}")
    return 0


def _run_x_poll(args: argparse.Namespace) -> int:
    from .social_x import check_x_signals, matches_to_jsonable

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1
    cfg = load_config(config_path)
    settings = Settings()

    if not cfg.social_x.enabled:
        print(
            "error: social_x.enabled=false in config. Set it to true and "
            "configure social_x.handles before running x-poll.",
            file=sys.stderr,
        )
        return 1
    if not settings.x_bearer_token:
        print(
            "error: X_BEARER_TOKEN missing from .env. "
            "Get one at https://developer.x.com/en/portal/dashboard.",
            file=sys.stderr,
        )
        return 1

    if args.check_bearer:
        return _check_x_bearer(cfg, settings)

    state_dir = Path(args.state_dir or cfg.state.dir)
    if args.reset:
        state_file = state_dir / "social_x.json"
        if state_file.exists():
            logger.warning("removing %s for --reset", state_file)
            state_file.unlink()

    effective = cfg.effective_social_x()
    if not effective.handles:
        print(
            "error: social_x.enabled=true but no handles found after expanding "
            "movies[].x_handles. Add at least one handle (under social_x.handles "
            "or under a movie's x_handles).",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "polling %d expanded x handle entries (deduped per-handle inside)",
        len(effective.handles),
    )
    result = check_x_signals(
        effective,
        settings.x_bearer_token,
        state_dir,
    )

    payload = {
        "handles_polled": result.handles_polled,
        "handles_failed": result.handles_failed,
        "errors": result.errors,
        "matches": matches_to_jsonable(result.matches),
    }
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.handles_failed == 0 else 1


def _check_x_bearer(cfg: Any, settings: Any) -> int:  # type: ignore[misc]
    """Validate X_BEARER_TOKEN with one cheap users/by/username call.

    Picks the first expanded handle from the effective social_x config so
    we exercise the same auth path the watcher uses, without touching the
    tweets endpoint (which is the rate-limited expensive one).
    """
    from .social_x import XApiError, XClient

    effective = cfg.effective_social_x()
    if not effective.handles:
        print(
            "error: --check-bearer needs at least one configured handle "
            "(social_x.handles[] or movies[].x_handles[]).",
            file=sys.stderr,
        )
        return 1
    handle = effective.handles[0].handle.lstrip("@")

    try:
        client = XClient(settings.x_bearer_token)
        user_id = client.get_user_id(handle)
    except XApiError as e:
        print(f"X API rejected the lookup: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — surface httpx + auth errors verbatim
        print(
            f"X bearer check failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: bearer token resolved @{handle} -> user_id={user_id}")
    return 0


def _run_dump_review(args: argparse.Namespace) -> int:
    """Capture a review-page DOM + screenshot to a JSON fixture.

    Reuses the purchaser's browser-session helper so the captured DOM
    matches what ``run_scripted_purchase`` would actually see (same
    persistent profile, same headless mode, same viewport). The fixture
    file is consumed by ``tests/test_review_fixtures.py`` to lock the
    invariant against real Fandango copy.
    """
    from datetime import UTC, datetime

    from .purchaser import _REVIEW_SNAPSHOT_JS, _browser_session

    config_path = _resolve_config_path(args.config)
    if config_path.is_file():
        cfg = load_config(config_path)
        browser_cfg = cfg.browser
    else:
        logger.warning(
            "no config found at %s; using default BrowserConfig",
            config_path,
        )
        browser_cfg = BrowserConfig(
            headless=False,
            user_data_dir="./browser-profile",
            viewport=ViewportConfig(),
        )
    if args.headed:
        browser_cfg = browser_cfg.model_copy(update={"headless": False})

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.name}.json"
    png_path = out_dir / f"{args.name}.png"

    if json_path.exists():
        print(
            f"error: fixture already exists: {json_path}. Pick a different "
            f"--name or delete the file first.",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "navigating to %s headless=%s wait_ms=%d",
        args.url,
        browser_cfg.headless,
        args.wait_ms,
    )

    with _browser_session(browser_cfg) as (_pw, context, _browser):
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(args.wait_ms)

        snapshot_raw = page.evaluate(_REVIEW_SNAPSHOT_JS)
        snapshot = snapshot_raw if isinstance(snapshot_raw, dict) else {}
        page_url = page.url
        page.screenshot(path=str(png_path), full_page=True)

    fixture = {
        "name": args.name,
        "captured_at": datetime.now(UTC).isoformat(),
        "source_url": args.url,
        "final_url": page_url,
        "screenshot": png_path.name,
        "snapshot": {
            "title": snapshot.get("title", ""),
            "bodyText": snapshot.get("bodyText", ""),
        },
        # Filled in by the human after capture: the plan-shaped expectations
        # the invariant test will assert against. See test_review_fixtures.py
        # for the schema.
        "expected": {
            "should_pass_invariant": True,
            "plan": {
                "target_name": "TODO",
                "theater_name": "TODO",
                "showtime_label": "TODO",
                "showtime_url": args.url,
                "format_tag": "IMAX_70MM",
                "auditorium": 1,
                "seat_priority": ["TODO"],
                "quantity": 1,
            },
            "invariant": {
                "require_total_equals": "$0.00",
                "require_benefit_phrase_any": [],
                "require_theater_match": True,
                "require_showtime_match": True,
                "require_seat_match": False,
            },
        },
    }
    json_path.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"wrote fixture: {json_path}")
    print(f"wrote screenshot: {png_path}")
    print(
        "next: edit the 'expected' block in the JSON to reflect the actual "
        "plan + invariant rules; then `uv run pytest "
        "tests/test_review_fixtures.py` will gate on it."
    )
    return 0


def _run_refs(args: argparse.Namespace) -> int:
    from .reference_pages import REFERENCE_PAGE_KEYS, REFERENCE_PAGES, get_reference_page

    if args.key is not None:
        if args.key not in REFERENCE_PAGE_KEYS:
            print(
                f"error: unknown reference key {args.key!r}. "
                f"known: {list(REFERENCE_PAGE_KEYS)}",
                file=sys.stderr,
            )
            return 1
        pages = [get_reference_page(args.key)]
    else:
        pages = [REFERENCE_PAGES[k] for k in REFERENCE_PAGE_KEYS]

    if args.output == "json":
        payload = [p.model_dump(mode="json") for p in pages]
        json.dump(payload if args.key is None else payload[0], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    for p in pages:
        cw = "citywalk" if p.citywalk_priority else "-"
        print(
            f"{p.key}\t{p.expected_schema}\t{cw}\t{p.url}",
        )
    return 0


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.command == "once":
        return _run_once(args)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
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

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":
    raise SystemExit(main())

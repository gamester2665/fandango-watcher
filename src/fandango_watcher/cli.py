"""Command-line interface.

Subcommands mirror the phased plan:

* ``once``         -- Phase 2: single crawl, print JSON, exit
* ``watch``        -- Phase 3: long poll loop with /healthz
* ``test-notify``  -- Phase 3: exercise SMS + email
* ``login``        -- Phase 5: headed first-run login (warms the persistent profile)
* ``test-purchase``-- Phase 4: dry-run the purchase planner against a live URL or
                       a saved JSON fixture, prints the resulting ``PurchasePlan``

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
from typing import Sequence

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

    return parser


# -----------------------------------------------------------------------------
# Subcommand handlers
# -----------------------------------------------------------------------------


def _resolve_config_path(explicit: str | None) -> Path:
    candidate = explicit or os.environ.get("WATCHER_CONFIG") or "config.yaml"
    return Path(candidate)


def _run_once(args: argparse.Namespace) -> int:
    # Imported lazily so `argparse --help` and stub subcommands don't pay the
    # cost of starting up Playwright.
    from .watcher import crawl_target

    if args.url:
        # Ad-hoc mode: synthesize a minimal target + browser config so the
        # user can `fandango-watcher once --url <URL>` without a config file.
        target = TargetConfig(name="adhoc", url=args.url)
        browser_cfg = BrowserConfig(
            headless=not args.headed,
            user_data_dir="./browser-profile",
            viewport=ViewportConfig(),
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

        browser_cfg = cfg.browser
        if args.headed:
            browser_cfg = browser_cfg.model_copy(update={"headless": False})

        citywalk_anchor = cfg.theater.fandango_theater_anchor
        screenshot_dir = (
            None if args.no_screenshot else Path(cfg.screenshots.dir)
        )

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
    )


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
    if args.command == "test-notify":
        return _run_test_notify(args)
    if args.command == "login":
        return _run_login(args)
    if args.command == "test-purchase":
        return _run_test_purchase(args)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI subcommand handlers (invoked from :mod:`fandango_watcher.cli`)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..config import (
    BrowserConfig,
    NotifyConfig,
    Settings,
    TargetConfig,
    ViewportConfig,
    load_config,
    plain_secret,
)

logger = logging.getLogger("fandango_watcher")

def _resolve_config_path(explicit: str | None) -> Path:
    """Resolve YAML config path.

    If ``config.yaml`` / ``WATCHER_CONFIG`` is missing, fall back to
    ``config.example.yaml`` in the **current working directory**, or
    ``/app/config.example.yaml`` in Docker (image ships a copy). This avoids a
    dead dashboard on a fresh clone before ``cp config.example.yaml config.yaml``,
    without breaking tests that run from an empty temp directory (no silent
    load of a repo-wide example via ``__file__``).
    """
    candidate = explicit or os.environ.get("WATCHER_CONFIG") or "config.yaml"
    p = Path(candidate)
    if p.is_file():
        return p.resolve()
    for fb in (Path("config.example.yaml"), Path("/app/config.example.yaml")):
        if fb.is_file():
            logger.info(
                "using bundled example config %s (config %s not found)",
                fb,
                p,
            )
            return fb.resolve()
    return p.resolve()


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
    from ..watcher import crawl_target

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

    direct_meta = None
    has_format_filter_override = (
        args.format_filter_selector is not None
        or args.format_filter_label is not None
        or args.format_filter_timeout_ms is not None
    )
    if (
        cfg_for_state is not None
        and cfg_for_state.direct_api.enabled
        and not has_format_filter_override
    ):
        from ..direct_api_detect import detect_target_direct_api

        try:
            logger.info("direct API detection target=%s url=%s", target.name, target.url)
            direct_result = detect_target_direct_api(target, cfg_for_state)
            result = direct_result.parsed
            direct_meta = direct_result.meta
        except Exception:
            if not cfg_for_state.direct_api.fallback_to_browser:
                raise
            logger.warning(
                "direct API detection failed; falling back to browser target=%s",
                target.name,
                exc_info=True,
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
    else:
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
        from ..state import load_target_state, save_target_state, transition

        assert cfg_for_state is not None
        state_dir = Path(cfg_for_state.state.dir)
        prev = load_target_state(state_dir, target.name)
        tr = transition(prev, result)
        state = tr.state
        if direct_meta is not None:
            state = state.model_copy(
                update={
                    "direct_api_last_status": direct_meta.status,
                    "direct_api_last_used": direct_meta.used_direct_api,
                    "direct_api_last_fallback": direct_meta.used_browser_fallback,
                    "direct_api_last_inspected_dates": direct_meta.inspected_dates,
                    "direct_api_last_formats_seen": direct_meta.formats_seen,
                    "direct_api_last_unknown_formats": direct_meta.unknown_formats,
                    "direct_api_last_matching_hashes": direct_meta.matching_showtime_hashes,
                    "direct_api_last_drift_warning": direct_meta.drift_warning,
                }
            )
            tr = tr.model_copy(update={"state": state})
        written = save_target_state(state_dir, state)
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

    from ..loop import install_signal_handlers, run_watch

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1

    cfg = load_config(config_path)
    if (
        args.format_filter_selector is not None
        or args.format_filter_label is not None
        or args.format_filter_timeout_ms is not None
    ):
        cfg = cfg.model_copy(
            update={
                "targets": [
                    _apply_format_filter_cli_overrides(t, args)
                    for t in cfg.targets
                ],
            },
        )
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

    bind_port = (
        args.healthz_port
        if args.healthz_port is not None
        else settings.healthz_port
    )
    healthz_port = None if args.no_healthz else bind_port

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

    from ..dashboard import DashboardData, DashboardPaths
    from ..healthz import Heartbeat, start_healthz_server
    from ..loop import install_signal_handlers

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

    bind_port = args.port if args.port is not None else settings.healthz_port
    try:
        ctx = start_healthz_server(
            hb,
            host=args.host,
            port=bind_port,
            dashboard_data=dd,
        )
        dd.public_host = args.host
        dd.public_port = ctx.port
    except OSError as e:
        print(
            f"error: failed to bind dashboard on {args.host}:{bind_port}: {e}\n"
            f"hint: another dashboard process may still be holding the port.\n"
            f"  windows: netstat -ano | findstr :{bind_port}\n"
            f"           taskkill /F /PID <pid>\n"
            f"  posix:   lsof -nP -iTCP:{bind_port} -sTCP:LISTEN\n"
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


def _run_api_drift(args: argparse.Namespace) -> int:
    from ..fandango_api import FandangoApiClient, drift_check

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1

    cfg = load_config(config_path)
    with FandangoApiClient(
        base_url=cfg.direct_api.base_url,
        theater_id=cfg.direct_api.theater_id,
        chain_code=cfg.direct_api.chain_code,
        timeout=cfg.direct_api.timeout_seconds,
    ) as client:
        report = drift_check(client, max_dates=args.max_dates)

    if args.output == "json":
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print(f"Direct API drift check: {'ok' if report.get('ok') else 'failed'}")
        print(f"Calendar dates: {report.get('calendar_date_count', 0)}")
        print("Inspected dates: " + ", ".join(report.get("inspected_dates") or []))
        formats = report.get("format_names_seen") or []
        print("Formats seen: " + (", ".join(str(x) for x in formats) or "none"))
        print("Showtime counts:")
        counts = report.get("showtime_count_by_date") or {}
        buyable_counts = report.get("buyable_count_by_date") or {}
        for showtime_date, count in counts.items():
            buyable = buyable_counts.get(showtime_date)
            print(f"  {showtime_date}: {count} showtimes, {buyable} buyable")
    return 0 if report.get("ok") else 1


def _run_test_notify(args: argparse.Namespace) -> int:
    from ..notify import NotificationMessage, build_notifier

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
    from ..login import DEFAULT_LOGIN_URL, run_login

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
    from ..models import validate_page_data
    from ..purchase import plan_purchase

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1
    cfg = load_config(config_path)

    if (
        args.stub
        and cfg.purchase.mode == "full_auto"
        and not args.allow_stub_with_full_auto
    ):
        print(
            "error: --stub is not allowed when purchase.mode is full_auto "
            "(use hold_and_confirm or notify_only for checkout drills, or pass "
            "--allow-stub-with-full-auto).",
            file=sys.stderr,
        )
        return 1

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
        from ..watcher import crawl_target

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
        if args.stub:
            print(
                "error: --stub requires a purchase plan (purchase.enabled, "
                "CityWalk bookable showtime, seat_priority for the format).",
                file=sys.stderr,
            )
            return 1
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

    payload: dict[str, Any] = {
        "plan": plan.model_dump(mode="json"),
        "release_schema": str(parsed.release_schema),
    }
    if args.stub:
        from ..purchaser import run_scripted_purchase

        attempt = run_scripted_purchase(
            plan,
            browser_cfg=cfg.browser,
            purchase_cfg=cfg.purchase,
            per_purchase_dir=Path(cfg.screenshots.per_purchase_dir),
            hold_for_confirm=True,
            settings=Settings(),
            agent_fallback_cfg=cfg.agent_fallback,
        )
        payload["purchase_attempt"] = attempt.model_dump(mode="json")

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


def _plain_x_bearer(settings: Settings) -> str:
    return plain_secret(settings.x_bearer_token).strip()


def _generated_x_bearer(settings: Settings) -> str:
    from ..social_x import generate_app_only_bearer_token

    return generate_app_only_bearer_token(
        plain_secret(settings.x_api_key).strip(),
        plain_secret(settings.x_api_key_secret).strip(),
    )


def _run_x_poll(args: argparse.Namespace) -> int:
    from ..social_x import check_x_signals, matches_to_jsonable

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        return 1
    cfg = load_config(config_path)
    settings = Settings()

    if args.check_bearer:
        return _check_x_bearer(cfg, settings)

    if not cfg.social_x.enabled:
        print(
            "error: social_x.enabled=false in config. Set it to true and "
            "configure social_x.handles before running x-poll.",
            file=sys.stderr,
        )
        return 1
    bearer_token = _plain_x_bearer(settings)
    if not bearer_token:
        if (
            plain_secret(settings.x_api_key).strip()
            and plain_secret(settings.x_api_key_secret).strip()
        ):
            try:
                bearer_token = _generated_x_bearer(settings)
            except Exception as e:  # noqa: BLE001
                print(
                    "error: X_BEARER_TOKEN missing and generating one from "
                    f"X_API_KEY/X_API_KEY_SECRET failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return 1
        else:
            print(
                "error: X_BEARER_TOKEN missing from .env. "
                "Get one at https://developer.x.com/en/portal/dashboard.",
                file=sys.stderr,
            )
            return 1

    if not bearer_token:
        print(
            "error: X_BEARER_TOKEN missing from .env. "
            "Get one at https://developer.x.com/en/portal/dashboard.",
            file=sys.stderr,
        )
        return 1

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
        bearer_token,
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


def _check_x_bearer(cfg: Any, settings: Any) -> int:
    """Validate X_BEARER_TOKEN with one cheap users/by/username call.

    Picks the first expanded handle from the effective social_x config so
    we exercise the same auth path the watcher uses, without touching the
    tweets endpoint (which is the rate-limited expensive one).
    """
    from ..social_x import XApiError, XClient

    effective = cfg.effective_social_x()
    if not effective.handles:
        print(
            "error: --check-bearer needs at least one configured handle "
            "(social_x.handles[] or movies[].x_handles[]).",
            file=sys.stderr,
        )
        return 1
    handle = effective.handles[0].handle.lstrip("@")

    attempts: list[tuple[str, str]] = []
    bearer = _plain_x_bearer(settings)
    if bearer:
        attempts.append(("X_BEARER_TOKEN", bearer))

    if (
        plain_secret(settings.x_api_key).strip()
        and plain_secret(settings.x_api_key_secret).strip()
    ):
        try:
            attempts.append((
                "generated from X_API_KEY/X_API_KEY_SECRET",
                _generated_x_bearer(settings),
            ))
        except Exception as e:  # noqa: BLE001
            print(
                "X API key/secret bearer generation failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    if not attempts:
        print(
            "error: set X_BEARER_TOKEN or X_API_KEY/X_API_KEY_SECRET in .env.",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for label, token in attempts:
        try:
            client = XClient(token)
            user_id = client.get_user_id(handle)
        except XApiError as e:
            failures.append(f"{label}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — surface httpx + auth errors verbatim
            failures.append(f"{label}: {type(e).__name__}: {e}")
            continue
        print(f"OK: {label} resolved @{handle} -> user_id={user_id}")
        return 0

    print("X API rejected every available app-only credential:", file=sys.stderr)
    for failure in failures:
        print(f"- {failure}", file=sys.stderr)
    return 1


def _run_dump_review(args: argparse.Namespace) -> int:
    """Capture a review-page DOM + screenshot to a JSON fixture.

    Reuses the purchaser's browser-session helper so the captured DOM
    matches what ``run_scripted_purchase`` would actually see (same
    persistent profile, same headless mode, same viewport). The fixture
    file is consumed by ``tests/test_review_fixtures.py`` to lock the
    invariant against real Fandango copy.
    """
    from datetime import UTC, datetime

    from ..purchaser import _browser_session, _review_snapshot_js

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

        snapshot_raw = page.evaluate(_review_snapshot_js())
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


def _run_doctor(args: argparse.Namespace) -> int:
    """Check config path, YAML validity, and common env/channel mismatches."""
    from ..notify import build_notifier

    config_path = _resolve_config_path(args.config)
    if not config_path.is_file():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        return 1
    try:
        cfg = load_config(config_path)
    except Exception as e:
        print(f"error: invalid config: {e}", file=sys.stderr)
        return 1

    settings = Settings()
    notifier = build_notifier(cfg.notify, settings)
    configured = set(cfg.notify.channels)
    active = set(notifier.channel_names)
    missing_creds = sorted(configured - active)

    warnings: list[str] = []
    infos: list[str] = []

    if missing_creds:
        warnings.append(
            "notify channels are listed in YAML but credentials are incomplete "
            f"(skipped at runtime): {missing_creds}"
        )
    if not configured:
        infos.append(
            "notify.channels is empty — transition notifications will not send."
        )

    if cfg.purchase.mode == "full_auto":
        warnings.append(
            "purchase.mode is full_auto — checkout can complete without manual "
            "confirmation when the invariant passes."
        )

    if cfg.social_x.enabled and not plain_secret(settings.x_bearer_token).strip():
        warnings.append(
            "social_x.enabled is true but X_BEARER_TOKEN is empty — x-poll will fail."
        )

    profile = Path(cfg.browser.user_data_dir)
    if not profile.is_dir():
        infos.append(
            f"browser profile directory does not exist yet ({profile}); "
            "run `fandango-watcher login` to create and warm it."
        )
    elif not any(profile.iterdir()):
        infos.append(
            f"browser profile at {profile} is empty; run `login` to warm cookies."
        )

    payload: dict[str, Any] = {
        "ok": True,
        "config_path": str(config_path),
        "purchase_mode": cfg.purchase.mode,
        "warnings": warnings,
        "infos": infos,
        "notify": {
            "configured_channels": sorted(configured),
            "active_channels": sorted(active),
        },
    }

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"config: {config_path}")
    print(f"purchase.mode: {cfg.purchase.mode}")
    print(
        "notify: "
        f"configured={sorted(configured) or '[]'}; "
        f"active (env-ready)={sorted(active) or '[]'}"
    )
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for i in infos:
        print(f"note: {i}")
    return 0


def _run_refs(args: argparse.Namespace) -> int:
    from ..reference_pages import REFERENCE_PAGE_KEYS, REFERENCE_PAGES, get_reference_page

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

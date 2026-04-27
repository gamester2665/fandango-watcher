"""Long-running watch loop.

Responsibilities:

1. Load per-target state from disk (``state.py``).
2. On each tick, crawl every target in one shared browser context
   (``watcher.crawl_targets_in_tick``), or use an injected ``crawl_fn`` in tests.
   and pipe the parsed result through :func:`state.transition` /
   :func:`state.record_error`.
3. Emit notifications for any resulting events that are enabled in
   ``cfg.notify.on_events``.
4. Sleep for a jittered interval drawn from ``cfg.poll.{min,max}_seconds``,
   with exponential backoff on error streaks capped at
   ``cfg.poll.error_backoff_cap_seconds``.
5. Update a shared :class:`~.healthz.Heartbeat` so ``/healthz`` reflects
   liveness.
6. Exit cleanly on SIGTERM / SIGINT.

Dependency-injection parameters (``crawl_fn``, ``notifier``, ``sleep_fn``,
``purchase_fn``, ``purchase_artifacts_dir``, ``stop_event``, ``max_ticks``)
exist purely so ``tests/test_loop.py`` can drive this function without
Playwright, SMTP, or wall-clock sleeps.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .artifacts import prune_artifact_trees
from .config import NotifyConfig, PurchaseConfig, Settings, WatcherConfig, plain_secret
from .dashboard import DashboardData, DashboardPaths
from .healthz import HealthzContext, Heartbeat, start_healthz_server
from .models import ParsedPageData
from .notify import (
    ChannelResult,
    FanOutNotifier,
    NotificationMessage,
    build_notifier,
)
from .purchase import PurchaseAttempt, PurchaseOutcome, plan_purchase
from .purchaser import run_scripted_purchase
from .social_x import XSignalMatch, check_x_signals
from .state import (
    Event,
    TargetState,
    TransitionResult,
    load_target_state,
    record_error,
    save_target_state,
    transition,
)
from .watcher import crawl_targets_in_tick

logger = logging.getLogger(__name__)

ERROR_STREAK_THRESHOLD = 5

# ``crawl_target`` signature kept loose here — the test suite substitutes a
# lightweight callable with the same keyword arguments.
CrawlFn = Callable[..., ParsedPageData]
SleepFn = Callable[[float], None]
PurchaseFn = Callable[..., PurchaseAttempt]


# -----------------------------------------------------------------------------
# Message construction
# -----------------------------------------------------------------------------


def _email_attachments_from_paths(
    paths: list[str],
    *,
    max_n: int,
    max_bytes: int,
) -> list[tuple[str, Path]]:
    """Pick existing files under ``max_bytes`` for SMTP MIME attachments."""
    out: list[tuple[str, Path]] = []
    for raw in paths[:max_n]:
        p = Path(raw)
        try:
            if p.is_file() and p.stat().st_size <= max_bytes:
                out.append((p.name, p))
        except OSError:
            continue
    return out


def _schema_value(parsed: ParsedPageData) -> str:
    # ``release_schema`` may be a StrEnum or plain str depending on
    # ``use_enum_values``; normalize for display.
    schema = parsed.release_schema
    return getattr(schema, "value", schema)


def build_notification(
    event: str,
    *,
    target_name: str,
    target_url: str,
    parsed: ParsedPageData | None = None,
    error: BaseException | None = None,
    error_streak: int | None = None,
    purchase_cfg: PurchaseConfig | None = None,
    notify: NotifyConfig | None = None,
) -> NotificationMessage:
    """Pure formatter. Kept separate from the loop so tests can assert on
    exact notification contents without running the orchestrator.

    When ``purchase_cfg`` is supplied and the event is
    ``release_transition_bad_to_good``, the planner runs against
    ``parsed`` and the chosen showtime / seats are appended to the body so
    the SMS/email itself is actionable (tap the URL, pick the listed seats).
    """
    if event == Event.RELEASE_TRANSITION_BAD_TO_GOOD:
        assert parsed is not None, "release_transition_bad_to_good needs parsed"
        subject = f"Tickets live: {target_name}"
        body_lines = [
            f"Target: {target_name}",
            f"URL: {target_url}",
            f"Release schema: {_schema_value(parsed)}",
            f"Theaters: {parsed.theater_count}  Showtimes: {parsed.showtime_count}",
            (
                f"CityWalk: present={parsed.citywalk_present} "
                f"showtimes={parsed.citywalk_showtime_count} "
                f"formats={list(parsed.citywalk_formats_seen)}"
            ),
        ]
        if parsed.ticket_url:
            body_lines.append(f"Tickets: {parsed.ticket_url}")

        if purchase_cfg is not None:
            plan = plan_purchase(
                parsed, target_name=target_name, purchase_cfg=purchase_cfg
            )
            if plan is not None:
                fmt = getattr(plan.format_tag, "value", plan.format_tag)
                body_lines.extend([
                    "",
                    f"Plan: {fmt} @ {plan.theater_name}",
                    f"Showtime: {plan.showtime_label}",
                    f"Buy: {plan.showtime_url}",
                    (
                        f"Seats (priority, aud {plan.auditorium}): "
                        f"{', '.join(plan.seat_priority)}"
                    ),
                    f"Mode: {purchase_cfg.mode} (max_qty={plan.quantity})",
                ])
            elif purchase_cfg.enabled:
                body_lines.extend([
                    "",
                    "Plan: no CityWalk showtime matched seat_priority "
                    "(check formats / sold out?)",
                ])
        attachments: list[tuple[str, Path]] = []
        if (
            notify is not None
            and notify.attach_screenshots_to_email
            and parsed.screenshot_path
        ):
            p = Path(parsed.screenshot_path)
            try:
                if p.is_file() and p.stat().st_size <= notify.email_max_attachment_bytes:
                    attachments.append((p.name, p))
            except OSError:
                pass
        return NotificationMessage(
            event=event,
            subject=subject,
            body="\n".join(body_lines),
            email_attachments=attachments,
        )

    if event == Event.WATCHER_STUCK_ON_ERROR_STREAK:
        subject = f"Watcher stuck: {target_name}"
        streak = error_streak if error_streak is not None else "?"
        body = (
            f"{streak} consecutive errors on target={target_name} "
            f"url={target_url}. Last error: {error!r}"
        )
        return NotificationMessage(event=event, subject=subject, body=body)

    # Fallback (Phase 4 events, etc.): let the caller format body separately.
    return NotificationMessage(event=event, subject=event, body=event)


def build_social_x_notification(
    match: XSignalMatch,
    *,
    target_url: str | None = None,
) -> NotificationMessage:
    """Format one X tweet match into a soft-hint notification.

    Explicitly labeled "X HINT" so a reader of the SMS / email can never
    confuse this with a hard ``Tickets live`` Fandango alert.
    """
    label = match.label or f"@{match.handle}"
    subject = f"X hint: {label}"
    body_lines = [
        "X HINT (advisory only — Fandango is still source of truth)",
        f"Account: @{match.handle}{f' ({match.label})' if match.label else ''}",
        f"Matched: {', '.join(match.matched_keywords)}",
    ]
    if match.created_at:
        body_lines.append(f"Posted: {match.created_at}")
    body_lines.extend([
        f"Tweet: {match.url}",
        "",
        match.text,
    ])
    if target_url:
        body_lines.extend([
            "",
            f"Watching: {target_url}",
        ])
    return NotificationMessage(
        event=Event.SOCIAL_X_MATCH,
        subject=subject,
        body="\n".join(body_lines),
    )


def _outcome_str(outcome: PurchaseOutcome | str) -> str:
    return outcome if isinstance(outcome, str) else outcome.value


def build_purchase_outcome_notification(
    attempt: PurchaseAttempt,
    *,
    target_name: str,
    target_url: str,
    notify: NotifyConfig | None = None,
) -> tuple[str, NotificationMessage]:
    """Map a finished :class:`~.purchase.PurchaseAttempt` to ``(event, msg)``."""
    ov = _outcome_str(attempt.outcome)

    if ov == PurchaseOutcome.SUCCESS.value:
        event = Event.PURCHASE_SUCCEEDED
        subject = f"Purchase succeeded: {target_name}"
    elif ov == PurchaseOutcome.HELD_FOR_CONFIRM.value:
        event = Event.PURCHASE_HELD_FOR_CONFIRM
        subject = f"Purchase held for confirm: {target_name}"
    elif ov == PurchaseOutcome.HALTED_INVARIANT.value:
        event = Event.PURCHASE_HALTED_INVARIANT
        subject = f"Purchase halted (invariant): {target_name}"
    elif ov == PurchaseOutcome.HALTED_PREFERRED_SOLD_OUT.value:
        event = Event.PURCHASE_HALTED_PREFERRED_SOLD_OUT
        subject = f"Purchase halted (seats): {target_name}"
    elif ov == PurchaseOutcome.FAILED_SCRIPTED.value:
        event = Event.PURCHASE_FAILED_SCRIPTED
        subject = f"Purchase failed (scripted): {target_name}"
    else:
        event = Event.PURCHASE_FAILED_SCRIPTED
        subject = f"Purchase finished ({ov}): {target_name}"

    body_lines = [
        f"Target: {target_name}",
        f"Watch URL: {target_url}",
        f"Showtime buy URL: {attempt.plan.showtime_url}",
        f"Outcome: {ov}",
    ]
    if attempt.halt_reason:
        body_lines.append(f"Halt: {attempt.halt_reason}")
    if attempt.error_message:
        body_lines.append(f"Error: {attempt.error_message}")
    if attempt.invariant_result is not None and not attempt.invariant_result.ok:
        body_lines.append(
            "Invariant failed: " + "; ".join(attempt.invariant_result.reasons_failed)
        )
    if attempt.screenshots:
        body_lines.append(f"Screenshots ({len(attempt.screenshots)}): see purchase-attempts/")
        body_lines.extend(attempt.screenshots[:5])

    attachments: list[tuple[str, Path]] = []
    if (
        notify is not None
        and notify.attach_screenshots_to_email
        and attempt.screenshots
    ):
        attachments = _email_attachments_from_paths(
            list(attempt.screenshots),
            max_n=notify.email_max_attachments,
            max_bytes=notify.email_max_attachment_bytes,
        )

    return event, NotificationMessage(
        event=event,
        subject=subject,
        body="\n".join(body_lines),
        email_attachments=attachments,
    )


# -----------------------------------------------------------------------------
# Dispatch helpers
# -----------------------------------------------------------------------------


def _emit_events(
    notifier: FanOutNotifier,
    *,
    result: TransitionResult,
    cfg: WatcherConfig,
    target_name: str,
    target_url: str,
    parsed: ParsedPageData | None,
    error: BaseException | None,
) -> list[ChannelResult]:
    all_results: list[ChannelResult] = []
    for event in result.events:
        if event not in cfg.notify.on_events:
            logger.debug("event %s not in notify.on_events; skipping", event)
            continue
        msg = build_notification(
            event,
            target_name=target_name,
            target_url=target_url,
            parsed=parsed,
            error=error,
            error_streak=result.state.consecutive_errors,
            purchase_cfg=cfg.purchase,
            notify=cfg.notify,
        )
        logger.info(
            "notifying event=%s target=%s channels=%s",
            event,
            target_name,
            notifier.channel_names,
        )
        all_results.extend(notifier.send(msg))
    return all_results


def _emit_purchase_outcome(
    notifier: FanOutNotifier,
    cfg: WatcherConfig,
    *,
    attempt: PurchaseAttempt,
    target_name: str,
    target_url: str,
) -> list[ChannelResult]:
    event, msg = build_purchase_outcome_notification(
        attempt,
        target_name=target_name,
        target_url=target_url,
        notify=cfg.notify,
    )
    if event not in cfg.notify.on_events:
        logger.debug("purchase event %s not in notify.on_events; skipping", event)
        return []
    logger.info(
        "notifying purchase outcome event=%s target=%s channels=%s",
        event,
        target_name,
        notifier.channel_names,
    )
    return notifier.send(msg)


# -----------------------------------------------------------------------------
# Sleep / backoff
# -----------------------------------------------------------------------------


def _emit_social_x_matches(
    notifier: FanOutNotifier,
    cfg: WatcherConfig,
    matches: list[XSignalMatch],
) -> list[ChannelResult]:
    if Event.SOCIAL_X_MATCH not in cfg.notify.on_events:
        if matches:
            logger.debug(
                "social_x produced %d matches but social_x_match not in on_events",
                len(matches),
            )
        return []
    target_by_name = {t.name: t for t in cfg.targets}
    results: list[ChannelResult] = []
    for m in matches:
        url = (
            target_by_name[m.target_name].url
            if m.target_name and m.target_name in target_by_name
            else None
        )
        msg = build_social_x_notification(m, target_url=url)
        logger.info(
            "notifying social_x event handle=@%s tweet=%s channels=%s",
            m.handle,
            m.tweet_id,
            notifier.channel_names,
        )
        results.extend(notifier.send(msg))
    return results


def _maybe_poll_social_x(
    *,
    cfg: WatcherConfig,
    settings: Settings,
    state_dir: Path,
    notifier: FanOutNotifier,
    next_poll_at: datetime,
    rng: random.Random,
    now: datetime,
    poll_fn: Callable[..., object] | None = None,
) -> datetime:
    """If due, run one X poll and emit notifications. Returns next due time.

    All exceptions are swallowed (logged) — a failing X poll must never
    interfere with the Fandango watch tick. ``poll_fn`` is injected by
    tests; default is :func:`check_x_signals`.
    """
    if not cfg.social_x.enabled:
        # Push the marker far enough into the future that we don't even
        # check on every tick when X is off.
        return now + _x_interval(cfg, rng)
    if now < next_poll_at:
        return next_poll_at

    impl = poll_fn if poll_fn is not None else check_x_signals
    try:
        # ``effective_social_x`` merges movies[].x_handles into the poll set
        # so the user only has to declare each handle once (under its movie).
        result = impl(
            cfg.effective_social_x(),
            plain_secret(settings.x_bearer_token),
            state_dir,
            now=now,
        )
    except Exception:  # noqa: BLE001 — must never break the Fandango loop
        logger.exception("social_x poll raised; will retry next interval")
        return now + _x_interval(cfg, rng)

    matches = getattr(result, "matches", []) or []
    rl_at = getattr(result, "rate_limit_reset_at", None)
    logger.info(
        "social_x poll done: %d matches, %d handles polled, %d failed",
        len(matches),
        getattr(result, "handles_polled", 0),
        getattr(result, "handles_failed", 0),
    )
    if matches:
        _emit_social_x_matches(notifier, cfg, matches)
    base_next = now + _x_interval(cfg, rng)
    if rl_at is not None and rl_at > now:
        return max(base_next, rl_at + timedelta(seconds=2))
    return base_next


def _x_interval(cfg: WatcherConfig, rng: random.Random) -> timedelta:
    from datetime import timedelta

    seconds = rng.uniform(cfg.social_x.min_seconds, cfg.social_x.max_seconds)
    return timedelta(seconds=seconds)


def _next_sleep_seconds(
    *,
    min_seconds: int,
    max_seconds: int,
    backoff_multiplier: float,
    cap_seconds: int,
    consecutive_errors: int,
    rng: random.Random,
) -> float:
    """Jittered sleep with exponential backoff.

    ``consecutive_errors == 0`` -> uniform(min, max) (the normal cadence).
    Each additional error multiplies the sleep by ``backoff_multiplier``,
    capped at ``cap_seconds``.
    """
    base = rng.uniform(min_seconds, max_seconds)
    if consecutive_errors <= 0:
        return min(base, float(cap_seconds))
    factor = backoff_multiplier ** max(0, consecutive_errors - 1)
    return min(base * factor, float(cap_seconds))


# -----------------------------------------------------------------------------
# Signal handling
# -----------------------------------------------------------------------------


def _rotate_purchases_jsonl(path: Path, *, keep_rotated: int) -> None:
    """Shift ``purchases.jsonl`` -> ``.1``, ``.1`` -> ``.2``, … up to ``keep_rotated``.

    Files beyond ``keep_rotated`` are dropped. ``keep_rotated == 0`` means truncate
    the active file with no archive copy. Errors are logged and swallowed so an
    audit-rotation hiccup never crashes the watch loop.
    """
    try:
        if keep_rotated > 0:
            oldest = path.with_suffix(path.suffix + f".{keep_rotated}")
            if oldest.exists():
                try:
                    oldest.unlink()
                except OSError:
                    logger.debug("rotate: unlink %s failed", oldest, exc_info=True)
            for i in range(keep_rotated - 1, 0, -1):
                src = path.with_suffix(path.suffix + f".{i}")
                dst = path.with_suffix(path.suffix + f".{i + 1}")
                if src.exists():
                    try:
                        src.replace(dst)
                    except OSError:
                        logger.debug("rotate: %s -> %s failed", src, dst, exc_info=True)
            try:
                path.replace(path.with_suffix(path.suffix + ".1"))
            except OSError:
                logger.debug("rotate: %s -> .1 failed", path, exc_info=True)
        else:
            try:
                path.unlink()
            except OSError:
                logger.debug("rotate: unlink active %s failed", path, exc_info=True)
    except Exception:  # noqa: BLE001 — rotation must never break the loop
        logger.warning("purchases.jsonl rotation failed", exc_info=True)


def append_purchase_jsonl(
    state_dir: Path,
    row: dict[str, object],
    *,
    max_bytes: int | None = None,
    keep_rotated: int = 3,
) -> None:
    """Append one JSON line to ``state/purchases.jsonl`` for auditing.

    When ``max_bytes`` is set and the file exceeds that size after the write,
    rotate to ``purchases.jsonl.1`` … ``.<keep_rotated>`` and start fresh. Pass
    ``max_bytes=None`` (default) to disable rotation entirely.
    """
    path = state_dir / "purchases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    if max_bytes is not None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > max_bytes:
            _rotate_purchases_jsonl(path, keep_rotated=keep_rotated)


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Wire SIGTERM/SIGINT to flip ``stop_event``.

    Safe to call on Windows (where SIGTERM is absent) and off the main
    thread (where ``signal.signal`` raises ValueError).
    """

    def _handler(signum: int, frame: object) -> None:
        logger.info("signal %d received; stopping watch loop", signum)
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Off the main thread or not supported on this OS.
            logger.debug("cannot install handler for %s on this runtime", sig_name)


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------


def run_watch(
    cfg: WatcherConfig,
    settings: Settings,
    *,
    state_dir: Path,
    screenshot_dir: Path | None,
    stop_event: threading.Event | None = None,
    crawl_fn: CrawlFn | None = None,
    notifier: FanOutNotifier | None = None,
    healthz_port: int | None = 8787,
    healthz_host: str = "127.0.0.1",
    sleep_fn: SleepFn | None = None,
    max_ticks: int | None = None,
    rng: random.Random | None = None,
    heartbeat: Heartbeat | None = None,
    purchase_fn: PurchaseFn | None = None,
    purchase_artifacts_dir: Path | None = None,
    social_x_poll_fn: Callable[..., object] | None = None,
    open_browser: bool = True,
    dashboard_refresh_seconds: int = 10,
) -> int:
    """Run the watch loop until ``stop_event`` is set or ``max_ticks`` is hit.

    Returns the process exit code: 0 on clean shutdown.
    """
    local_stop = stop_event if stop_event is not None else threading.Event()
    crawl_impl: CrawlFn | None = crawl_fn
    use_shared_playwright = crawl_fn is None
    notify_impl: FanOutNotifier = (
        notifier if notifier is not None else build_notifier(cfg.notify, settings)
    )
    rng_impl = rng if rng is not None else random.Random()
    hb = heartbeat if heartbeat is not None else Heartbeat()
    purchase_impl: PurchaseFn = (
        purchase_fn if purchase_fn is not None else run_scripted_purchase
    )

    if sleep_fn is None:
        # Default sleep honors stop_event so SIGTERM interrupts mid-sleep.
        def default_sleep(seconds: float) -> None:
            local_stop.wait(seconds)

        sleep_impl: SleepFn = default_sleep
    else:
        sleep_impl = sleep_fn

    target_states: dict[str, TargetState] = {
        t.name: load_target_state(state_dir, t.name) for t in cfg.targets
    }

    # First X poll runs on the very first tick (so a manual `watch` invocation
    # immediately surfaces any pending hints), then settles into the jittered
    # ``social_x.{min,max}_seconds`` cadence.
    next_x_poll_at: datetime = datetime.now(UTC)

    healthz_ctx: HealthzContext | None = None
    if healthz_port is not None:
        try:
            dash_paths = DashboardPaths.from_config(cfg)
            dashboard_data = DashboardData(
                cfg=cfg,
                paths=dash_paths,
                heartbeat=hb,
                settings=settings,
                refresh_seconds=max(0, int(dashboard_refresh_seconds)),
            )
            healthz_ctx = start_healthz_server(
                hb,
                host=healthz_host,
                port=healthz_port,
                dashboard_data=dashboard_data,
            )
            dashboard_data.public_host = healthz_host
            dashboard_data.public_port = healthz_ctx.port
            base = f"http://{healthz_host}:{healthz_ctx.port}/"
            if open_browser:
                import webbrowser

                try:
                    # Prefer the same browser window when the OS honors it (avoids
                    # a new tab on every restart if a dashboard tab is already open).
                    webbrowser.open(base, new=0)
                except Exception:  # noqa: BLE001
                    logger.debug("webbrowser.open failed", exc_info=True)
        except OSError:
            logger.exception(
                "failed to start healthz on %s:%d; continuing without",
                healthz_host,
                healthz_port,
            )

    tick = 0
    try:
        while not local_stop.is_set():
            tick += 1
            now_tick = datetime.now(UTC)
            with hb.mutex:
                hb.last_tick_at = now_tick
                hb.total_ticks += 1

            tick_batch: dict[str, ParsedPageData | BaseException] = {}
            if use_shared_playwright:
                tick_batch = crawl_targets_in_tick(
                    cfg.targets,
                    browser_cfg=cfg.browser,
                    citywalk_anchor=cfg.theater.fandango_theater_anchor,
                    screenshot_dir=screenshot_dir,
                )

            tick_had_successful_crawl = False
            for target in cfg.targets:
                if local_stop.is_set():
                    break
                prev = target_states[target.name]
                if use_shared_playwright:
                    raw_out = tick_batch.get(target.name)
                    assert raw_out is not None
                    if isinstance(raw_out, BaseException):
                        e = raw_out
                        with hb.mutex:
                            hb.total_errors += 1
                        logger.exception(
                            "crawl failed target=%s url=%s",
                            target.name,
                            target.url,
                        )
                        err_result = record_error(
                            prev,
                            e,
                            error_streak_threshold=ERROR_STREAK_THRESHOLD,
                        )
                        target_states[target.name] = err_result.state
                        save_target_state(state_dir, err_result.state)
                        _emit_events(
                            notify_impl,
                            result=err_result,
                            cfg=cfg,
                            target_name=target.name,
                            target_url=target.url,
                            parsed=None,
                            error=e,
                        )
                        continue
                    parsed = raw_out
                    tick_had_successful_crawl = True
                else:
                    assert crawl_impl is not None
                    try:
                        parsed = crawl_impl(
                            target,
                            browser_cfg=cfg.browser,
                            citywalk_anchor=cfg.theater.fandango_theater_anchor,
                            screenshot_dir=screenshot_dir,
                        )
                        tick_had_successful_crawl = True
                    except Exception as e:  # noqa: BLE001
                        with hb.mutex:
                            hb.total_errors += 1
                        logger.exception(
                            "crawl failed target=%s url=%s",
                            target.name,
                            target.url,
                        )
                        err_result = record_error(
                            prev,
                            e,
                            error_streak_threshold=ERROR_STREAK_THRESHOLD,
                        )
                        target_states[target.name] = err_result.state
                        save_target_state(state_dir, err_result.state)
                        _emit_events(
                            notify_impl,
                            result=err_result,
                            cfg=cfg,
                            target_name=target.name,
                            target_url=target.url,
                            parsed=None,
                            error=e,
                        )
                        continue

                ok_result = transition(prev, parsed)
                target_states[target.name] = ok_result.state
                save_target_state(state_dir, ok_result.state)
                _emit_events(
                    notify_impl,
                    result=ok_result,
                    cfg=cfg,
                    target_name=target.name,
                    target_url=target.url,
                    parsed=parsed,
                    error=None,
                )

                if (
                    Event.RELEASE_TRANSITION_BAD_TO_GOOD in ok_result.events
                    and cfg.purchase.enabled
                    and cfg.purchase.mode in ("full_auto", "hold_and_confirm")
                ):
                    buy_plan = plan_purchase(
                        parsed,
                        target_name=target.name,
                        purchase_cfg=cfg.purchase,
                    )
                    if buy_plan is not None:
                        art_root = (
                            purchase_artifacts_dir
                            if purchase_artifacts_dir is not None
                            else Path(cfg.screenshots.per_purchase_dir)
                        )
                        try:
                            attempt = purchase_impl(
                                buy_plan,
                                browser_cfg=cfg.browser,
                                purchase_cfg=cfg.purchase,
                                per_purchase_dir=art_root,
                                hold_for_confirm=(
                                    cfg.purchase.mode == "hold_and_confirm"
                                ),
                                settings=settings,
                                agent_fallback_cfg=cfg.agent_fallback,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.exception(
                                "purchase_fn raised target=%s", target.name
                            )
                            attempt = PurchaseAttempt(
                                plan=buy_plan,
                                started_at=datetime.now(UTC),
                                finished_at=datetime.now(UTC),
                                outcome=PurchaseOutcome.FAILED_SCRIPTED,
                                error_message=f"{type(e).__name__}: {e}",
                            )
                        _emit_purchase_outcome(
                            notify_impl,
                            cfg,
                            attempt=attempt,
                            target_name=target.name,
                            target_url=target.url,
                        )
                        append_purchase_jsonl(
                            state_dir,
                            {
                                "target": target.name,
                                "at": datetime.now(UTC).isoformat(),
                                "attempt": attempt.model_dump(mode="json"),
                            },
                            max_bytes=cfg.purchase_audit.max_bytes,
                            keep_rotated=cfg.purchase_audit.keep_rotated,
                        )

            try:
                prune_artifact_trees(cfg)
            except Exception:  # noqa: BLE001
                logger.debug("artifact prune failed", exc_info=True)

            next_x_poll_at = _maybe_poll_social_x(
                cfg=cfg,
                settings=settings,
                state_dir=state_dir,
                notifier=notify_impl,
                next_poll_at=next_x_poll_at,
                rng=rng_impl,
                now=datetime.now(UTC),
                poll_fn=social_x_poll_fn,
            )

            if max_ticks is not None and tick >= max_ticks:
                logger.info("max_ticks=%d reached; stopping", max_ticks)
                break

            err_for_sleep = (
                0
                if tick_had_successful_crawl
                else max(
                    target_states[t.name].consecutive_errors for t in cfg.targets
                )
            )
            sleep_seconds = _next_sleep_seconds(
                min_seconds=cfg.poll.min_seconds,
                max_seconds=cfg.poll.max_seconds,
                backoff_multiplier=cfg.poll.error_backoff_multiplier,
                cap_seconds=cfg.poll.error_backoff_cap_seconds,
                consecutive_errors=err_for_sleep,
                rng=rng_impl,
            )
            logger.debug(
                "sleeping %.1fs (err_for_sleep=%d tick_had_ok=%s)",
                sleep_seconds,
                err_for_sleep,
                tick_had_successful_crawl,
            )
            sleep_impl(sleep_seconds)
    finally:
        if healthz_ctx is not None:
            try:
                healthz_ctx.stop()
            except Exception:  # noqa: BLE001 — best effort on shutdown
                logger.exception("error stopping healthz server")

    return 0

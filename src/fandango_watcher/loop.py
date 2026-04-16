"""Long-running watch loop.

Responsibilities:

1. Load per-target state from disk (``state.py``).
2. On each tick, crawl every target sequentially (``watcher.crawl_target``)
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

import logging
import random
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import PurchaseConfig, Settings, WatcherConfig
from .healthz import Heartbeat, HealthzContext, start_healthz_server
from .models import ParsedPageData
from .notify import (
    ChannelResult,
    FanOutNotifier,
    NotificationMessage,
    build_notifier,
)
from .purchase import PurchaseAttempt, PurchaseOutcome, plan_purchase
from .purchaser import run_scripted_purchase
from .state import (
    Event,
    TargetState,
    TransitionResult,
    load_target_state,
    record_error,
    save_target_state,
    transition,
)
from .watcher import crawl_target

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
        return NotificationMessage(
            event=event, subject=subject, body="\n".join(body_lines)
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


def _outcome_str(outcome: PurchaseOutcome | str) -> str:
    return outcome if isinstance(outcome, str) else outcome.value


def build_purchase_outcome_notification(
    attempt: PurchaseAttempt,
    *,
    target_name: str,
    target_url: str,
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

    return event, NotificationMessage(
        event=event, subject=subject, body="\n".join(body_lines)
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
        attempt, target_name=target_name, target_url=target_url
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
) -> int:
    """Run the watch loop until ``stop_event`` is set or ``max_ticks`` is hit.

    Returns the process exit code: 0 on clean shutdown.
    """
    local_stop = stop_event if stop_event is not None else threading.Event()
    crawl_impl: CrawlFn = crawl_fn if crawl_fn is not None else crawl_target
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

    healthz_ctx: HealthzContext | None = None
    if healthz_port is not None:
        try:
            healthz_ctx = start_healthz_server(
                hb, host=healthz_host, port=healthz_port
            )
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
            hb.last_tick_at = datetime.now(UTC)
            hb.total_ticks += 1

            max_streak_this_tick = 0

            for target in cfg.targets:
                if local_stop.is_set():
                    break
                prev = target_states[target.name]
                try:
                    parsed = crawl_impl(
                        target,
                        browser_cfg=cfg.browser,
                        citywalk_anchor=cfg.theater.fandango_theater_anchor,
                        screenshot_dir=screenshot_dir,
                    )
                except Exception as e:  # noqa: BLE001 — loop must survive per-target failures
                    hb.total_errors += 1
                    logger.exception(
                        "crawl failed target=%s url=%s", target.name, target.url
                    )
                    err_result = record_error(
                        prev, e, error_streak_threshold=ERROR_STREAK_THRESHOLD
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
                    max_streak_this_tick = max(
                        max_streak_this_tick, err_result.state.consecutive_errors
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

            if max_ticks is not None and tick >= max_ticks:
                logger.info("max_ticks=%d reached; stopping", max_ticks)
                break

            sleep_seconds = _next_sleep_seconds(
                min_seconds=cfg.poll.min_seconds,
                max_seconds=cfg.poll.max_seconds,
                backoff_multiplier=cfg.poll.error_backoff_multiplier,
                cap_seconds=cfg.poll.error_backoff_cap_seconds,
                consecutive_errors=max_streak_this_tick,
                rng=rng_impl,
            )
            logger.debug(
                "sleeping %.1fs (max_streak_this_tick=%d)",
                sleep_seconds,
                max_streak_this_tick,
            )
            sleep_impl(sleep_seconds)
    finally:
        if healthz_ctx is not None:
            try:
                healthz_ctx.stop()
            except Exception:  # noqa: BLE001 — best effort on shutdown
                logger.exception("error stopping healthz server")

    return 0

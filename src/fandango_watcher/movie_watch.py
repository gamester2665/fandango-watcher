"""Per-movie schedule SMS + pinned showtime monitoring."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings, WatcherConfig
from .fandango_api import FandangoApiClient
from .models import ParsedPageData, ReleaseSchema
from .movie_watch_state import (
    MovieWatchState,
    PinnedShowtime,
    load_movie_watch_state,
    save_movie_watch_state,
)
from .notify import FanOutNotifier, NotificationMessage
from .schedule_intel import (
    build_schedule_snapshot,
    classify_pin_against_records,
    extract_schedule_from_parsed,
    format_schedule_notification_body,
    pin_from_record,
    scan_movie_schedule_via_api,
    schedule_fingerprint,
)
from .state import Event

logger = logging.getLogger(__name__)


def _schema_value(parsed: ParsedPageData) -> str:
    schema = parsed.release_schema
    return schema.value if hasattr(schema, "value") else str(schema)


def _movie_has_citywalk_showtimes(parsed: ParsedPageData) -> bool:
    schema = _schema_value(parsed)
    if schema == ReleaseSchema.NOT_ON_SALE.value:
        return False
    return bool(parsed.citywalk_showtime_count and parsed.citywalk_showtime_count > 0)


def _movie_anchor_slug(movie_key: str) -> str:
    out: list[str] = []
    for char in movie_key:
        if char.isalnum() or char in ("_", "-"):
            out.append(char)
        elif char in " ./\\":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "movie"


def _dashboard_movie_url(
    settings: Settings, healthz_host: str, healthz_port: int, movie_key: str
) -> str:
    host = healthz_host if healthz_host != "0.0.0.0" else "127.0.0.1"
    slug = _movie_anchor_slug(movie_key)
    return f"http://{host}:{healthz_port}/#movie-{slug}"


def _primary_target_for_movie(cfg: WatcherConfig, movie_key: str):
    movie = next((m for m in cfg.movies if m.key == movie_key), None)
    if movie is None or not movie.fandango_targets:
        return None
    name = movie.fandango_targets[0]
    return next((t for t in cfg.targets if t.name == name), None)


def _collect_schedule_records(
    cfg: WatcherConfig,
    *,
    movie_key: str,
    parsed: ParsedPageData | None,
    api_client: FandangoApiClient | None,
    calendar_dates: list[str] | None,
) -> tuple[list[Any], list[str], str]:
    from .direct_api_detect import _wanted_formats
    from .fandango_api import FandangoShowtimeRecord

    target = _primary_target_for_movie(cfg, movie_key)
    if target is None:
        return [], [], "none"
    movie = cfg.movie_for_target(target.name)
    wanted = _wanted_formats(target, cfg)
    max_scan = cfg.schedule_notify.max_dates_scan

    if cfg.schedule_notify.full_calendar_scan and api_client is not None:
        records, inspected = scan_movie_schedule_via_api(
            target,
            cfg,
            client=api_client,
            calendar_dates=calendar_dates,
            max_dates=max_scan,
        )
        return records, inspected, "direct_api"

    if parsed is not None and _movie_has_citywalk_showtimes(parsed):
        records = extract_schedule_from_parsed(parsed, wanted_formats=wanted)
        return records, [], "parsed_page"

    return [], [], "none"


def process_movies_after_tick(
    cfg: WatcherConfig,
    settings: Settings,
    *,
    state_dir: Path,
    notifier: FanOutNotifier,
    tick_parsed_by_target: dict[str, ParsedPageData],
    api_client: FandangoApiClient | None,
    calendar_dates: list[str] | None,
    notified_keys: set[str],
    healthz_host: str = "127.0.0.1",
    healthz_port: int = 8787,
) -> None:
    """Schedule reveal + pin checks once per tick (per movie)."""
    if not cfg.movies:
        return

    for movie in cfg.movies:
        if not movie.fandango_targets:
            continue
        parsed: ParsedPageData | None = None
        for tname in movie.fandango_targets:
            candidate = tick_parsed_by_target.get(tname)
            if candidate is None:
                continue
            if parsed is None or (
                (candidate.citywalk_showtime_count or 0)
                > (parsed.citywalk_showtime_count or 0)
            ):
                parsed = candidate

        has_signal = parsed is not None and _movie_has_citywalk_showtimes(parsed)
        need_pins = cfg.pin_watch.enabled and bool(
            load_movie_watch_state(state_dir, movie.key).pinned_showtimes
        )
        mstate = load_movie_watch_state(state_dir, movie.key)

        records, inspected, source = _collect_schedule_records(
            cfg,
            movie_key=movie.key,
            parsed=parsed if (has_signal or need_pins) else None,
            api_client=api_client if (has_signal or need_pins) else None,
            calendar_dates=calendar_dates,
        )

        if cfg.schedule_notify.enabled and records and has_signal:
            _maybe_schedule_notify(
                cfg,
                settings,
                state_dir=state_dir,
                notifier=notifier,
                movie_key=movie.key,
                movie_title=movie.title,
                mstate=mstate,
                records=records,
                inspected=inspected,
                source=source,
                notified_keys=notified_keys,
                healthz_host=healthz_host,
                healthz_port=healthz_port,
            )
            mstate = load_movie_watch_state(state_dir, movie.key)

        if cfg.pin_watch.enabled and mstate.pinned_showtimes and records:
            _check_pins(
                cfg,
                notifier=notifier,
                mstate=mstate,
                movie_title=movie.title,
                records=records,
                notified_keys=notified_keys,
                state_dir=state_dir,
            )


def _maybe_schedule_notify(
    cfg: WatcherConfig,
    settings: Settings,
    *,
    state_dir: Path,
    notifier: FanOutNotifier,
    movie_key: str,
    movie_title: str,
    mstate: MovieWatchState,
    records: list[Any],
    inspected: list[str],
    source: str,
    notified_keys: set[str],
    healthz_host: str,
    healthz_port: int,
) -> None:
    if Event.CITYWALK_SCHEDULE_REVEALED not in cfg.notify.on_events:
        return

    snapshot = build_schedule_snapshot(
        movie_key=movie_key,
        movie_title=movie_title,
        theater_name=cfg.theater.display_name,
        records=records,
        inspected_dates=inspected,
        source=source,
    )
    if not snapshot.days:
        return

    fp = schedule_fingerprint(snapshot)
    if mstate.schedule_notified_at is not None:
        if not cfg.schedule_notify.resend_on_schedule_change:
            return
        if mstate.schedule_fingerprint == fp:
            return

    dedupe_key = f"{Event.CITYWALK_SCHEDULE_REVEALED}:movie:{movie_key}"
    if dedupe_key in notified_keys:
        return
    notified_keys.add(dedupe_key)

    dash_url = _dashboard_movie_url(settings, healthz_host, healthz_port, movie_key)
    body = format_schedule_notification_body(
        snapshot,
        cfg=cfg.schedule_notify,
        dashboard_url=dash_url,
    )
    msg = NotificationMessage(
        event=Event.CITYWALK_SCHEDULE_REVEALED,
        subject=f"CityWalk schedule: {movie_title}",
        body=body,
    )
    logger.info("notifying citywalk_schedule_revealed movie=%s dates=%d", movie_key, len(snapshot.days))
    notifier.send(msg)

    mstate.schedule_notified_at = datetime.now(UTC)
    mstate.schedule_fingerprint = fp
    save_movie_watch_state(state_dir, mstate)


def _check_pins(
    cfg: WatcherConfig,
    *,
    notifier: FanOutNotifier,
    mstate: MovieWatchState,
    movie_title: str,
    records: list[Any],
    notified_keys: set[str],
    state_dir: Path,
) -> None:
    changed = False
    for pin in mstate.pinned_showtimes:
        status = classify_pin_against_records(pin, records)
        prev = mstate.pin_notify_status.get(pin.pin_id())
        pin_label = pin.label or f"{pin.date} {pin.time_label}"

        if status is None:
            if prev in ("listed", "live") and cfg.pin_watch.notify_on_gone:
                event = Event.PINNED_SHOWTIME_GONE
                if event in cfg.notify.on_events:
                    dedupe = f"{event}:{pin.pin_id()}"
                    if dedupe not in notified_keys:
                        notified_keys.add(dedupe)
                        _send_pin_msg(
                            notifier,
                            event=event,
                            movie_title=movie_title,
                            pin_label=pin_label,
                            body=f"Pinned showtime no longer listed:\n{pin_label}",
                        )
                mstate.pin_notify_status[pin.pin_id()] = "gone"
                changed = True
            continue

        if status == "listed" and cfg.pin_watch.notify_on_listed:
            event = Event.PINNED_SHOWTIME_LISTED
            if event in cfg.notify.on_events and prev != "listed":
                dedupe = f"{event}:{pin.pin_id()}"
                if dedupe not in notified_keys:
                    notified_keys.add(dedupe)
                    _send_pin_msg(
                        notifier,
                        event=event,
                        movie_title=movie_title,
                        pin_label=pin_label,
                        body=f"Pinned showtime is listed (not buyable yet):\n{pin_label}",
                    )
            if prev != status:
                mstate.pin_notify_status[pin.pin_id()] = status
                changed = True
            continue

        if status == "live" and cfg.pin_watch.notify_on_live:
            event = Event.PINNED_SHOWTIME_LIVE
            if event in cfg.notify.on_events and prev != "live":
                dedupe = f"{event}:{pin.pin_id()}"
                if dedupe not in notified_keys:
                    notified_keys.add(dedupe)
                    matched = [r for r in records if classify_pin_against_records(pin, [r]) == "live"]
                    url = matched[0].ticket_url if matched else pin.ticket_url
                    extra = f"\nBuy: {url}" if url else ""
                    _send_pin_msg(
                        notifier,
                        event=event,
                        movie_title=movie_title,
                        pin_label=pin_label,
                        body=f"Pinned showtime is BUYABLE:\n{pin_label}{extra}",
                    )
            if prev != status:
                mstate.pin_notify_status[pin.pin_id()] = status
                changed = True

    if changed:
        save_movie_watch_state(state_dir, mstate)


def _send_pin_msg(
    notifier: FanOutNotifier,
    *,
    event: str,
    movie_title: str,
    pin_label: str,
    body: str,
) -> None:
    msg = NotificationMessage(
        event=event,
        subject=f"Pin alert: {movie_title}",
        body=body,
    )
    logger.info("notifying %s movie=%s pin=%s", event, movie_title, pin_label)
    notifier.send(msg)


def set_movie_pins(
    state_dir: Path,
    movie_key: str,
    pins: list[PinnedShowtime],
) -> MovieWatchState:
    mstate = load_movie_watch_state(state_dir, movie_key)
    mstate.pinned_showtimes = pins
    save_movie_watch_state(state_dir, mstate)
    return mstate


def fetch_movie_schedule_json(
    cfg: WatcherConfig,
    movie_key: str,
    *,
    state_dir: Path,
    api_client: FandangoApiClient | None = None,
) -> dict[str, Any]:
    movie = next((m for m in cfg.movies if m.key == movie_key), None)
    if movie is None:
        raise ValueError(f"unknown movie key: {movie_key!r}")
    records, inspected, source = _collect_schedule_records(
        cfg,
        movie_key=movie_key,
        parsed=None,
        api_client=api_client,
        calendar_dates=None,
    )
    snapshot = build_schedule_snapshot(
        movie_key=movie_key,
        movie_title=movie.title,
        theater_name=cfg.theater.display_name,
        records=records,
        inspected_dates=inspected,
        source=source,
    )
    mstate = load_movie_watch_state(state_dir, movie_key)
    return {
        "ok": True,
        "movie_key": movie_key,
        "schedule": snapshot.model_dump(mode="json"),
        "pinned_showtimes": [
            p.model_dump(mode="json") for p in mstate.pinned_showtimes
        ],
        "schedule_notified_at": (
            mstate.schedule_notified_at.isoformat()
            if mstate.schedule_notified_at
            else None
        ),
    }

"""CityWalk schedule inventory + pinned showtime matching."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import ScheduleNotifyConfig, TargetConfig, WatcherConfig
from .direct_api_detect import _movie_matchers, _record_matches_target, _wanted_formats
from .fandango_api import (
    FandangoApiClient,
    FandangoShowtimeRecord,
    parse_showtime_records,
)
from .models import (
    FormatTag,
    FullReleasePageData,
    ParsedPageData,
    PartialReleasePageData,
    Showtime,
    ShowtimesDisclosedPageData,
)
from .movie_watch_state import PinnedShowtime


class ScheduleDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    times: list[str] = Field(default_factory=list)
    buyable_times: list[str] = Field(default_factory=list)
    records: list[dict[str, Any]] = Field(default_factory=list)


class MovieScheduleSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movie_key: str
    movie_title: str
    theater_name: str
    days: list[ScheduleDay] = Field(default_factory=list)
    inspected_dates: list[str] = Field(default_factory=list)
    source: str = "direct_api"


def _format_tag_str(value: FormatTag | str) -> str:
    return value.value if isinstance(value, FormatTag) else str(value)


def _time_label(record: FandangoShowtimeRecord) -> str:
    return (
        record.display_time
        or record.screen_reader_time
        or record.ticketing_date
        or "showtime"
    )


def scan_movie_schedule_via_api(
    target: TargetConfig,
    cfg: WatcherConfig,
    *,
    client: FandangoApiClient,
    calendar_dates: list[str] | None = None,
    max_dates: int | None = None,
) -> tuple[list[FandangoShowtimeRecord], list[str]]:
    """Full calendar scan for one movie (no early stop on first buyable)."""
    dates = calendar_dates if calendar_dates is not None else client.calendar_dates()
    limit = max_dates if max_dates is not None else cfg.direct_api.max_dates_per_tick
    scan_dates = dates[:limit]
    movie_id, movie_title = _movie_matchers(target, cfg)
    wanted = _wanted_formats(target, cfg)
    visible: list[FandangoShowtimeRecord] = []
    inspected: list[str] = []

    for showtime_date in scan_dates:
        inspected.append(showtime_date)
        payload = client.get_json(client.showtimes_url(showtime_date))
        records = parse_showtime_records(
            payload,
            theater_id=cfg.direct_api.theater_id,
            chain_code=cfg.direct_api.chain_code,
            requested_date=showtime_date,
        )
        for record in records:
            if _record_matches_target(
                record,
                movie_id=movie_id,
                movie_title=movie_title,
                wanted_formats=wanted,
                require_buyable=False,
            ):
                visible.append(record)
    return visible, inspected


def extract_schedule_from_parsed(
    parsed: ParsedPageData,
    *,
    wanted_formats: set[str],
) -> list[FandangoShowtimeRecord]:
    """Best-effort schedule from a classified page (often one date only)."""
    if not isinstance(
        parsed, (PartialReleasePageData, FullReleasePageData, ShowtimesDisclosedPageData)
    ):
        return []
    out: list[FandangoShowtimeRecord] = []
    for theater in parsed.theaters:
        if not theater.is_citywalk:
            continue
        for section in theater.format_sections:
            fmt_str = _format_tag_str(section.normalized_format)
            if wanted_formats and fmt_str not in wanted_formats:
                raw_labels = {section.label.upper().replace(" ", "_")}
                if not raw_labels.intersection(wanted_formats):
                    continue
            for st in section.showtimes:
                date = st.date_label or "unknown"
                out.append(
                    FandangoShowtimeRecord(
                        theater_id="",
                        chain_code="",
                        date=date,
                        movie_title=parsed.movie_title,
                        format_names=[section.label or fmt_str],
                        normalized_formats=[section.normalized_format],
                        display_time=st.label,
                        is_buyable=st.is_buyable,
                        ticket_url=st.ticket_url,
                        showtime_hash=None,
                    )
                )
    return out


def build_schedule_snapshot(
    *,
    movie_key: str,
    movie_title: str,
    theater_name: str,
    records: Iterable[FandangoShowtimeRecord],
    inspected_dates: list[str],
    source: str,
) -> MovieScheduleSnapshot:
    by_date: dict[str, list[FandangoShowtimeRecord]] = defaultdict(list)
    for record in records:
        by_date[record.date or "unknown"].append(record)

    days: list[ScheduleDay] = []
    for date in sorted(by_date):
        recs = by_date[date]
        times: list[str] = []
        buyable: list[str] = []
        payloads: list[dict[str, Any]] = []
        for rec in recs:
            label = _time_label(rec)
            fmt = rec.format_names[0] if rec.format_names else ""
            entry = f"{label} {fmt}".strip()
            times.append(entry)
            if rec.is_buyable:
                buyable.append(entry)
            payloads.append(
                {
                    "time_label": label,
                    "format_names": list(rec.format_names),
                    "normalized_formats": [
                        _format_tag_str(x) for x in rec.normalized_formats
                    ],
                    "is_buyable": rec.is_buyable,
                    "ticket_url": rec.ticket_url,
                    "showtime_hash": rec.showtime_hash,
                    "date": rec.date,
                }
            )
        days.append(
            ScheduleDay(
                date=date,
                times=_dedupe_preserve(times),
                buyable_times=_dedupe_preserve(buyable),
                records=payloads,
            )
        )
    return MovieScheduleSnapshot(
        movie_key=movie_key,
        movie_title=movie_title,
        theater_name=theater_name,
        days=days,
        inspected_dates=inspected_dates,
        source=source,
    )


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def schedule_fingerprint(snapshot: MovieScheduleSnapshot) -> str:
    payload = {
        "dates": [
            {"date": d.date, "times": d.times, "buyable": d.buyable_times}
            for d in snapshot.days
        ]
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def format_schedule_notification_body(
    snapshot: MovieScheduleSnapshot,
    *,
    cfg: ScheduleNotifyConfig,
    dashboard_url: str | None = None,
) -> str:
    lines = [
        f"{snapshot.movie_title} @ {snapshot.theater_name}",
        "CityWalk dates:",
    ]
    shown = snapshot.days[: cfg.max_dates_in_sms]
    for day in shown:
        if cfg.include_times and day.times:
            lines.append(f"  {day.date}: {', '.join(day.times)}")
        else:
            lines.append(f"  {day.date} ({len(day.times)} showtime(s))")
    extra = len(snapshot.days) - len(shown)
    if extra > 0:
        lines.append(f"  (+{extra} more date(s) on dashboard)")
    if dashboard_url:
        lines.append(f"Pick a showtime: {dashboard_url}#movie-{snapshot.movie_key}")
    return "\n".join(lines)


def record_matches_pin(
    record: FandangoShowtimeRecord,
    pin: PinnedShowtime,
) -> bool:
    if pin.showtime_hash and record.showtime_hash:
        return pin.showtime_hash == record.showtime_hash
    if pin.ticket_url and record.ticket_url:
        return pin.ticket_url == record.ticket_url
    pin_fmt = _format_tag_str(pin.format)
    rec_fmts = {_format_tag_str(x) for x in record.normalized_formats}
    if pin_fmt not in rec_fmts:
        return False
    pin_time = pin.time_label.strip().lower()
    rec_time = _time_label(record).strip().lower()
    if pin_time not in rec_time and rec_time not in pin_time:
        return False
    pin_date = pin.date.strip()
    rec_date = (record.date or "").strip()
    return pin_date == rec_date or pin_date in rec_date or rec_date in pin_date


def pin_from_record(record: FandangoShowtimeRecord) -> PinnedShowtime:
    fmt = record.normalized_formats[0] if record.normalized_formats else FormatTag.OTHER
    label = f"{record.date} {_time_label(record)} {_format_tag_str(fmt)}"
    return PinnedShowtime(
        date=record.date or "",
        time_label=_time_label(record),
        format=fmt,
        showtime_hash=record.showtime_hash,
        ticket_url=record.ticket_url,
        label=label.strip(),
    )


def classify_pin_against_records(
    pin: PinnedShowtime,
    records: Iterable[FandangoShowtimeRecord],
) -> str | None:
    """Return ``listed``, ``live``, or ``None`` if not present."""
    matched: list[FandangoShowtimeRecord] = [
        r for r in records if record_matches_pin(r, pin)
    ]
    if not matched:
        return None
    if any(r.is_buyable for r in matched):
        return "live"
    return "listed"


def find_pinned_showtime_in_parsed(
    parsed: ParsedPageData,
    pin: PinnedShowtime,
    *,
    wanted_formats: set[str],
) -> Showtime | None:
    records = extract_schedule_from_parsed(parsed, wanted_formats=wanted_formats)
    for record in records:
        if record_matches_pin(record, pin):
            return Showtime(
                label=_time_label(record),
                ticket_url=record.ticket_url,
                is_buyable=record.is_buyable,
                is_citywalk=True,
                date_label=record.date,
            )
    return None

"""Direct API detection adapter.

Turns normalized records from :mod:`fandango_watcher.fandango_api` into the
same ``ParsedPageData`` schema used by the Playwright DOM crawler.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import TargetConfig, WatcherConfig
from .fandango_api import (
    FandangoApiClient,
    FandangoApiError,
    FandangoShowtimeRecord,
    get_available_formats,
    parse_showtime_records,
)
from .models import (
    FormatFilter,
    FormatSection,
    FormatTag,
    FullReleasePageData,
    NotOnSalePageData,
    ParsedPageData,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
)


class DirectApiDetectionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    used_direct_api: bool = True
    used_browser_fallback: bool = False
    inspected_dates: list[str] = Field(default_factory=list)
    formats_seen: list[str] = Field(default_factory=list)
    unknown_formats: list[str] = Field(default_factory=list)
    matching_showtime_hashes: list[str] = Field(default_factory=list)
    buyable_count: int = 0
    drift_warning: str | None = None


class DirectApiDetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parsed: ParsedPageData
    meta: DirectApiDetectionMeta


def _dedupe(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _format_value(value: FormatTag | str) -> str:
    return value.value if isinstance(value, FormatTag) else str(value)


def _wanted_formats(target: TargetConfig, cfg: WatcherConfig) -> set[str]:
    if target.direct_api_formats:
        return {_format_value(value) for value in target.direct_api_formats}
    movie = cfg.movie_for_target(target.name)
    if movie is not None and movie.preferred_formats:
        return {_format_value(value) for value in movie.preferred_formats}
    return {
        _format_value(value)
        for value in [*cfg.formats.require, *cfg.formats.include]
    }


def _movie_matchers(target: TargetConfig, cfg: WatcherConfig) -> tuple[int | None, str | None]:
    movie = cfg.movie_for_target(target.name)
    movie_id = target.direct_api_movie_id
    if movie_id is None and movie is not None:
        movie_id = movie.fandango_movie_id
    title = target.direct_api_movie_title
    if title is None and movie is not None:
        title = movie.title
    return movie_id, title.lower() if title else None


def _record_matches_target(
    record: FandangoShowtimeRecord,
    *,
    movie_id: int | None,
    movie_title: str | None,
    wanted_formats: set[str],
) -> bool:
    if not record.is_buyable:
        return False
    if movie_id is not None and record.movie_id != movie_id:
        return False
    if movie_title is not None:
        title = (record.movie_title or "").lower()
        if movie_title not in title and title not in movie_title:
            return False
    if wanted_formats:
        raw = set(record.format_names)
        norm = {_format_value(value) for value in record.normalized_formats}
        if not raw.intersection(wanted_formats) and not norm.intersection(wanted_formats):
            return False
    return True


def _unknown_formats(records: Iterable[FandangoShowtimeRecord]) -> list[str]:
    values: list[str] = []
    for record in records:
        for raw, normalized in zip(record.format_names, record.normalized_formats, strict=False):
            normalized_value = _format_value(normalized)
            if normalized_value == FormatTag.OTHER.value and raw != "STANDARD":
                values.append(raw)
    return _dedupe(values)


def _section_for_format(
    format_name: str,
    records: list[FandangoShowtimeRecord],
) -> FormatSection:
    normalized = records[0].normalized_formats[0] if records else FormatTag.OTHER
    return FormatSection(
        label=format_name,
        normalized_format=normalized,
        attributes=_dedupe(
            attr for record in records if (attr := record.amenities)
        ),
        showtimes=[
            Showtime(
                label=record.display_time or record.ticketing_date or "showtime",
                ticket_url=record.ticket_url,
                is_buyable=record.is_buyable,
                is_citywalk=True,
                date_label=record.ticketing_date or record.date,
            )
            for record in records
        ],
    )


def _parsed_from_matches(
    *,
    target: TargetConfig,
    cfg: WatcherConfig,
    matches: list[FandangoShowtimeRecord],
    meta: DirectApiDetectionMeta,
) -> ParsedPageData:
    movie = cfg.movie_for_target(target.name)
    movie_title = (
        matches[0].movie_title
        if matches and matches[0].movie_title
        else movie.title if movie is not None else target.direct_api_movie_title
    )
    format_filters = [
        FormatFilter(label=fmt, normalized_format=FormatTag.OTHER)
        for fmt in meta.formats_seen
    ]
    if not matches:
        return NotOnSalePageData(
            url=target.url,
            page_title=movie_title or target.name,
            movie_title=movie_title,
            poster_url=movie.poster_url if movie is not None else None,
            format_filters_present=format_filters,
            schema_evidence=[
                "direct_api",
                "direct_api_no_matching_buyable_showtimes",
                f"direct_api_dates={len(meta.inspected_dates)}",
            ],
            theater_count=0,
            showtime_count=0,
            formats_seen=[],
            citywalk_present=False,
            citywalk_showtime_count=0,
            citywalk_formats_seen=[],
        )

    by_format: dict[str, list[FandangoShowtimeRecord]] = {}
    for record in matches:
        key = record.format_names[0] if record.format_names else "STANDARD"
        by_format.setdefault(key, []).append(record)

    sections = [
        _section_for_format(format_name, records)
        for format_name, records in by_format.items()
    ]
    formats_seen = _dedupe(section.normalized_format for section in sections)
    release_schema = (
        ReleaseSchema.FULL_RELEASE
        if len(matches) >= 20
        else ReleaseSchema.PARTIAL_RELEASE
    )
    payload: dict[str, Any] = {
        "release_schema": release_schema.value,
        "url": target.url,
        "page_title": movie_title or target.name,
        "movie_title": movie_title,
        "poster_url": movie.poster_url if movie is not None else None,
        "format_filters_present": [
            ff.model_dump(mode="json") for ff in format_filters
        ],
        "ticket_url": matches[0].ticket_url,
        "schema_evidence": [
            "direct_api",
            f"direct_api_dates={len(meta.inspected_dates)}",
            f"direct_api_matches={len(matches)}",
            *[f"direct_api_format={fmt}" for fmt in by_format],
                *[
                    f"direct_api_unknown_format={fmt}"
                    for fmt in meta.unknown_formats
                ],
        ],
        "theater_count": 1,
        "showtime_count": len(matches),
        "formats_seen": formats_seen,
        "citywalk_present": True,
        "citywalk_showtime_count": len(matches),
        "citywalk_formats_seen": formats_seen,
        "theaters": [
            TheaterListing(
                name=cfg.theater.display_name,
                is_citywalk=True,
                format_sections=sections,
            ).model_dump(mode="json")
        ],
    }
    if release_schema is ReleaseSchema.FULL_RELEASE:
        return FullReleasePageData.model_validate(payload)
    return PartialReleasePageData.model_validate(payload)


def detect_target_direct_api(
    target: TargetConfig,
    cfg: WatcherConfig,
    *,
    client: FandangoApiClient | None = None,
) -> DirectApiDetectionResult:
    owns_client = client is None
    api = client or FandangoApiClient(
        base_url=cfg.direct_api.base_url,
        theater_id=cfg.direct_api.theater_id,
        chain_code=cfg.direct_api.chain_code,
        timeout=cfg.direct_api.timeout_seconds,
    )
    try:
        calendar_dates = api.calendar_dates()
        scan_dates = calendar_dates[: cfg.direct_api.max_dates_per_tick]
        inspected_dates: list[str] = []
        meta = DirectApiDetectionMeta()
        movie_id, movie_title = _movie_matchers(target, cfg)
        wanted_formats = _wanted_formats(target, cfg)
        matches: list[FandangoShowtimeRecord] = []
        all_records: list[FandangoShowtimeRecord] = []

        for showtime_date in scan_dates:
            inspected_dates.append(showtime_date)
            payload = api.get_json(api.showtimes_url(showtime_date))
            meta.formats_seen = _dedupe([
                *meta.formats_seen,
                *get_available_formats(payload),
            ])
            records = parse_showtime_records(
                payload,
                theater_id=cfg.direct_api.theater_id,
                chain_code=cfg.direct_api.chain_code,
                requested_date=showtime_date,
            )
            all_records.extend(records)
            date_matches = [
                record
                for record in records
                if _record_matches_target(
                    record,
                    movie_id=movie_id,
                    movie_title=movie_title,
                    wanted_formats=wanted_formats,
                )
            ]
            matches.extend(date_matches)
            if date_matches and cfg.direct_api.stop_on_first_match:
                break

        meta.inspected_dates = inspected_dates
        meta.unknown_formats = _unknown_formats(all_records)
        meta.matching_showtime_hashes = _dedupe(
            record.showtime_hash
            for record in matches
            if record.showtime_hash is not None
        )
        meta.buyable_count = sum(1 for record in all_records if record.is_buyable)
        if meta.unknown_formats:
            meta.drift_warning = (
                "unknown direct API format(s): " + ", ".join(meta.unknown_formats)
            )
        parsed = _parsed_from_matches(
            target=target,
            cfg=cfg,
            matches=matches,
            meta=meta,
        )
        return DirectApiDetectionResult(parsed=parsed, meta=meta)
    except Exception as exc:
        if isinstance(exc, FandangoApiError):
            raise
        raise FandangoApiError(f"direct API detection failed: {exc}") from exc
    finally:
        if owns_client:
            api.close()

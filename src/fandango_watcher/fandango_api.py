"""Direct Fandango JSON API helpers.

This module intentionally keeps the direct API path independent from
Playwright. It builds the observed private endpoint URLs, fetches JSON with
browser-like headers, and normalizes raw showtime payloads into small records
the watcher and tests can reason about.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .detect import normalize_format_label
from .models import FormatTag

DEFAULT_BASE_URL = "https://www.fandango.com"
DEFAULT_THEATER_ID = "AAAWX"
DEFAULT_CHAIN_CODE = "AMC"
DEFAULT_ZIP_CODE = "91608"
DEFAULT_REFERER = (
    "https://www.fandango.com/"
    "universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
    "Referer": DEFAULT_REFERER,
}
TICKETS_HOST_PREFIX = "https://tickets.fandango.com/"
BUYABLE_TYPES = frozenset({"available"})

JsonObject = dict[str, Any]


class FandangoApiError(RuntimeError):
    """Raised when a direct Fandango API response is malformed or unavailable."""


class FandangoShowtimeRecord(BaseModel):
    """Normalized showtime extracted from ``theaterMovieShowtimes`` JSON."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    source: Literal["fandango_direct_api"] = "fandango_direct_api"
    theater_id: str
    chain_code: str
    date: str
    movie_id: int | str | None = None
    movie_title: str | None = None
    format_names: list[str] = Field(min_length=1)
    normalized_formats: list[FormatTag] = Field(min_length=1)
    display_time: str | None = None
    screen_reader_time: str | None = None
    ticketing_date: str | None = None
    showtime_hash: str | None = None
    availability_type: str | None = None
    expired: bool = False
    is_buyable: bool = False
    ticket_url: str | None = None
    variant_header: str | None = None
    amenities: str | None = None

    @field_validator("format_names", "normalized_formats")
    @classmethod
    def _dedupe_lists(cls, values: list[Any]) -> list[Any]:
        return _dedupe_preserve_order(values)


def _dedupe_preserve_order(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _date_string(value: str | date) -> str:
    return value.isoformat() if isinstance(value, date) else value


def build_calendar_url(
    theater_id: str = DEFAULT_THEATER_ID,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Build the observed theater calendar URL."""

    return f"{_base_url(base_url)}/napi/theaterCalendar/{theater_id.lower()}"


def build_showtimes_url(
    theater_id: str = DEFAULT_THEATER_ID,
    *,
    chain_code: str = DEFAULT_CHAIN_CODE,
    start_date: str | date,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Build the observed per-date theater showtimes URL."""

    query = urlencode(
        {
            "chainCode": chain_code,
            "startDate": _date_string(start_date),
            "isdesktop": "true",
            "partnerRestrictedTicketing": "",
        }
    )
    return f"{_base_url(base_url)}/napi/theaterMovieShowtimes/{theater_id.upper()}?{query}"


def build_nearby_theaters_url(
    zip_code: str = DEFAULT_ZIP_CODE,
    *,
    limit: int = 7,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Build the observed nearby-theaters discovery URL."""

    query = urlencode({"limit": limit, "zipCode": zip_code})
    return f"{_base_url(base_url)}/napi/nearbyTheaters?{query}"


def _require_object(value: Any, path: str) -> JsonObject:
    if not isinstance(value, dict):
        raise FandangoApiError(f"expected object at {path}, got {type(value).__name__}")
    return value


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def parse_calendar_dates(payload: Mapping[str, Any]) -> list[str]:
    """Return validated ``YYYY-MM-DD`` date strings from a calendar response."""

    dates = payload.get("showtimeDates")
    if dates is None:
        return []
    if not isinstance(dates, list):
        raise FandangoApiError("expected showtimeDates to be a list")
    out: list[str] = []
    for raw in dates:
        if not isinstance(raw, str) or len(raw) != 10:
            raise FandangoApiError(f"invalid showtime date: {raw!r}")
        out.append(raw)
    return out


def get_available_formats(payload: Mapping[str, Any]) -> list[str]:
    """Return ``viewModel.formats`` from a showtimes response."""

    view_model = _require_object(payload.get("viewModel"), "viewModel")
    return [
        value
        for value in _list_or_empty(view_model.get("formats"))
        if isinstance(value, str) and value.strip()
    ]


def _format_names_for_showtime(showtime: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for raw in _list_or_empty(showtime.get("filmFormat")):
        if not isinstance(raw, dict):
            continue
        filter_name = raw.get("filterName")
        if not isinstance(filter_name, str):
            continue
        filter_name = filter_name.strip()
        if filter_name:
            names.append(filter_name)
    return _dedupe_preserve_order(names) or ["STANDARD"]


def _is_buyable(showtime: Mapping[str, Any], ticket_url: str | None) -> bool:
    return (
        ticket_url is not None
        and ticket_url.startswith(TICKETS_HOST_PREFIX)
        and showtime.get("expired") is False
        and showtime.get("type") in BUYABLE_TYPES
    )


def iter_showtime_records(
    payload: Mapping[str, Any],
    *,
    theater_id: str = DEFAULT_THEATER_ID,
    chain_code: str = DEFAULT_CHAIN_CODE,
    requested_date: str | date | None = None,
) -> Iterator[FandangoShowtimeRecord]:
    """Yield normalized showtimes from a ``theaterMovieShowtimes`` payload."""

    view_model = _require_object(payload.get("viewModel"), "viewModel")
    response_date = requested_date or view_model.get("date")
    if response_date is None:
        raise FandangoApiError("missing response date; pass requested_date explicitly")
    record_date = _date_string(response_date)

    theater = view_model.get("theater")
    if isinstance(theater, dict):
        details = theater.get("details")
        if isinstance(details, dict):
            theater_id = str(details.get("id") or theater_id)
            chain_code = str(details.get("chainCode") or chain_code)

    for movie in _list_or_empty(view_model.get("movies")):
        if not isinstance(movie, dict):
            continue
        movie_id = movie.get("id")
        movie_title = movie.get("title") if isinstance(movie.get("title"), str) else None
        for variant in _list_or_empty(movie.get("variants")):
            if not isinstance(variant, dict):
                continue
            variant_header = (
                variant.get("filmFormatHeader")
                if isinstance(variant.get("filmFormatHeader"), str)
                else None
            )
            for group in _list_or_empty(variant.get("amenityGroups")):
                if not isinstance(group, dict):
                    continue
                amenities = (
                    group.get("amenityString")
                    if isinstance(group.get("amenityString"), str)
                    else None
                )
                for showtime in _list_or_empty(group.get("showtimes")):
                    if not isinstance(showtime, dict):
                        continue
                    format_names = _format_names_for_showtime(showtime)
                    ticket_url = (
                        showtime.get("ticketingJumpPageURL")
                        if isinstance(showtime.get("ticketingJumpPageURL"), str)
                        else None
                    )
                    normalized_formats = [
                        normalize_format_label(format_name)
                        for format_name in format_names
                    ]
                    yield FandangoShowtimeRecord(
                        theater_id=theater_id,
                        chain_code=chain_code,
                        date=record_date,
                        movie_id=movie_id if isinstance(movie_id, int | str) else None,
                        movie_title=movie_title,
                        format_names=format_names,
                        normalized_formats=normalized_formats,
                        display_time=(
                            showtime.get("date")
                            if isinstance(showtime.get("date"), str)
                            else None
                        ),
                        screen_reader_time=(
                            showtime.get("screenReaderTime")
                            if isinstance(showtime.get("screenReaderTime"), str)
                            else None
                        ),
                        ticketing_date=(
                            showtime.get("ticketingDate")
                            if isinstance(showtime.get("ticketingDate"), str)
                            else None
                        ),
                        showtime_hash=(
                            showtime.get("showtimeHashCode")
                            if isinstance(showtime.get("showtimeHashCode"), str)
                            else None
                        ),
                        availability_type=(
                            showtime.get("type")
                            if isinstance(showtime.get("type"), str)
                            else None
                        ),
                        expired=showtime.get("expired") is True,
                        is_buyable=_is_buyable(showtime, ticket_url),
                        ticket_url=ticket_url,
                        variant_header=variant_header,
                        amenities=amenities,
                    )


def parse_showtime_records(
    payload: Mapping[str, Any],
    *,
    theater_id: str = DEFAULT_THEATER_ID,
    chain_code: str = DEFAULT_CHAIN_CODE,
    requested_date: str | date | None = None,
) -> list[FandangoShowtimeRecord]:
    """Return all normalized showtime records from a showtimes payload."""

    return list(
        iter_showtime_records(
            payload,
            theater_id=theater_id,
            chain_code=chain_code,
            requested_date=requested_date,
        )
    )


def matching_records(
    records: Iterable[FandangoShowtimeRecord],
    wanted_formats: set[FormatTag | str],
) -> list[FandangoShowtimeRecord]:
    """Filter normalized records by raw or normalized format names."""

    wanted = {str(value) for value in wanted_formats}
    return [
        record
        for record in records
        if wanted.intersection(record.format_names)
        or wanted.intersection(str(value) for value in record.normalized_formats)
    ]


class FandangoApiClient:
    """Small synchronous client for the observed private Fandango JSON endpoints."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        theater_id: str = DEFAULT_THEATER_ID,
        chain_code: str = DEFAULT_CHAIN_CODE,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.theater_id = theater_id
        self.chain_code = chain_code
        self.headers = {**DEFAULT_HEADERS, **dict(headers or {})}
        self._client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FandangoApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_json(self, url: str) -> JsonObject:
        response = self._client.get(url, headers=self.headers)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise FandangoApiError(f"non-JSON Fandango response from {url}") from exc
        return _require_object(data, "$")

    def calendar_url(self) -> str:
        return build_calendar_url(self.theater_id, base_url=self.base_url)

    def showtimes_url(self, start_date: str | date) -> str:
        return build_showtimes_url(
            self.theater_id,
            chain_code=self.chain_code,
            start_date=start_date,
            base_url=self.base_url,
        )

    def nearby_theaters_url(self, zip_code: str = DEFAULT_ZIP_CODE, *, limit: int = 7) -> str:
        return build_nearby_theaters_url(zip_code, limit=limit, base_url=self.base_url)

    def calendar_dates(self) -> list[str]:
        return parse_calendar_dates(self.get_json(self.calendar_url()))

    def showtime_records(self, start_date: str | date) -> list[FandangoShowtimeRecord]:
        payload = self.get_json(self.showtimes_url(start_date))
        return parse_showtime_records(
            payload,
            theater_id=self.theater_id,
            chain_code=self.chain_code,
            requested_date=start_date,
        )


def drift_check(
    client: FandangoApiClient | None = None,
    *,
    max_dates: int = 1,
) -> JsonObject:
    """Fetch live data and return a compact schema/format drift report."""

    owns_client = client is None
    api = client or FandangoApiClient()
    try:
        dates = api.calendar_dates()
        inspected_dates = dates[:max(0, max_dates)]
        records_by_date: dict[str, list[FandangoShowtimeRecord]] = {}
        formats_by_date: dict[str, list[str]] = {}
        for showtime_date in inspected_dates:
            payload = api.get_json(api.showtimes_url(showtime_date))
            formats_by_date[showtime_date] = get_available_formats(payload)
            records_by_date[showtime_date] = parse_showtime_records(
                payload,
                theater_id=api.theater_id,
                chain_code=api.chain_code,
                requested_date=showtime_date,
            )
        format_names = sorted(
            {
                name
                for records in records_by_date.values()
                for record in records
                for name in record.format_names
            }
        )
        return {
            "ok": bool(dates),
            "calendar_date_count": len(dates),
            "inspected_dates": inspected_dates,
            "formats_by_date": formats_by_date,
            "showtime_count_by_date": {
                showtime_date: len(records)
                for showtime_date, records in records_by_date.items()
            },
            "buyable_count_by_date": {
                showtime_date: sum(1 for record in records if record.is_buyable)
                for showtime_date, records in records_by_date.items()
            },
            "format_names_seen": format_names,
        }
    finally:
        if owns_client:
            api.close()

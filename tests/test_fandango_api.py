"""Tests for the direct Fandango JSON API helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fandango_watcher.fandango_api import (
    DEFAULT_CHAIN_CODE,
    DEFAULT_THEATER_ID,
    FandangoApiClient,
    FandangoApiError,
    build_calendar_url,
    build_nearby_theaters_url,
    build_showtimes_url,
    drift_check,
    get_available_formats,
    matching_records,
    parse_calendar_dates,
    parse_showtime_records,
)
from fandango_watcher.models import FormatTag

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fandango_api"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_url_builders_use_observed_routes_and_params() -> None:
    assert (
        build_calendar_url("AAAWX")
        == "https://www.fandango.com/napi/theaterCalendar/aaawx"
    )
    assert (
        build_showtimes_url(
            "aaawx",
            chain_code="AMC",
            start_date="2026-04-28",
        )
        == "https://www.fandango.com/napi/theaterMovieShowtimes/AAAWX?"
        "chainCode=AMC&startDate=2026-04-28&isdesktop=true&partnerRestrictedTicketing="
    )
    assert (
        build_nearby_theaters_url("91608", limit=7)
        == "https://www.fandango.com/napi/nearbyTheaters?limit=7&zipCode=91608"
    )


def test_parse_calendar_dates_validates_shape() -> None:
    assert parse_calendar_dates(_fixture("calendar_citywalk.json")) == [
        "2026-04-28",
        "2026-04-29",
        "2026-07-17",
    ]

    with pytest.raises(FandangoApiError, match="showtimeDates"):
        parse_calendar_dates({"showtimeDates": "2026-04-28"})


def test_get_available_formats_reads_view_model_formats() -> None:
    payload = _fixture("showtimes_citywalk_mixed.json")
    assert get_available_formats(payload) == [
        "IMAX",
        "3D",
        "IMAX 70MM",
        "MYSTERY FORMAT",
    ]


def test_parse_showtime_records_normalizes_formats_and_buyable_flags() -> None:
    payload = _fixture("showtimes_citywalk_mixed.json")
    records = parse_showtime_records(payload)

    by_hash = {record.showtime_hash: record for record in records}
    assert set(by_hash) == {
        "v2-standard",
        "v2-expired",
        "v2-missing-url",
        "v2-imax",
        "v2-imax70mm",
        "v2-3d",
        "v2-unknown-format",
    }

    standard = by_hash["v2-standard"]
    assert standard.format_names == ["STANDARD"]
    assert standard.normalized_formats == ["STANDARD"]
    assert standard.is_buyable is True

    expired = by_hash["v2-expired"]
    assert expired.expired is True
    assert expired.is_buyable is False

    missing_url = by_hash["v2-missing-url"]
    assert missing_url.ticket_url is None
    assert missing_url.is_buyable is False

    imax = by_hash["v2-imax"]
    assert imax.format_names == ["IMAX"]
    assert imax.normalized_formats == ["IMAX"]
    assert imax.movie_title == "Michael (2026)"
    assert imax.variant_header == "Premium Format"

    imax_70mm = by_hash["v2-imax70mm"]
    assert imax_70mm.format_names == ["IMAX 70MM"]
    assert imax_70mm.normalized_formats == ["IMAX_70MM"]

    three_d = by_hash["v2-3d"]
    assert three_d.format_names == ["3D"]
    assert three_d.normalized_formats == ["THREE_D"]

    unknown = by_hash["v2-unknown-format"]
    assert unknown.format_names == ["MYSTERY FORMAT"]
    assert unknown.normalized_formats == ["OTHER"]


def test_matching_records_accepts_raw_or_normalized_format_names() -> None:
    records = parse_showtime_records(_fixture("showtimes_citywalk_mixed.json"))

    assert [r.showtime_hash for r in matching_records(records, {"IMAX 70MM"})] == [
        "v2-imax70mm"
    ]
    assert [r.showtime_hash for r in matching_records(records, {FormatTag.THREE_D})] == [
        "v2-3d"
    ]


def test_parse_showtime_records_requires_view_model() -> None:
    with pytest.raises(FandangoApiError, match="viewModel"):
        parse_showtime_records({})


def test_client_uses_mockable_httpx_transport() -> None:
    calendar_url = build_calendar_url(DEFAULT_THEATER_ID)
    showtimes_url = build_showtimes_url(
        DEFAULT_THEATER_ID,
        chain_code=DEFAULT_CHAIN_CODE,
        start_date="2026-04-28",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Mozilla/5.0" in request.headers["User-Agent"]
        if str(request.url) == calendar_url:
            return httpx.Response(200, json=_fixture("calendar_citywalk.json"))
        if str(request.url) == showtimes_url:
            return httpx.Response(200, json=_fixture("showtimes_citywalk_mixed.json"))
        return httpx.Response(404, json={"error": "not found"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = FandangoApiClient(http_client=http_client)

    assert client.calendar_dates() == ["2026-04-28", "2026-04-29", "2026-07-17"]
    records = client.showtime_records("2026-04-28")
    assert len(records) == 7
    assert sum(1 for record in records if record.is_buyable) == 5

    http_client.close()


def test_drift_check_reports_compact_live_contract_summary_with_mock_client() -> None:
    calendar_url = build_calendar_url(DEFAULT_THEATER_ID)
    showtimes_url = build_showtimes_url(
        DEFAULT_THEATER_ID,
        chain_code=DEFAULT_CHAIN_CODE,
        start_date="2026-04-28",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == calendar_url:
            return httpx.Response(200, json=_fixture("calendar_citywalk.json"))
        if str(request.url) == showtimes_url:
            return httpx.Response(200, json=_fixture("showtimes_citywalk_mixed.json"))
        return httpx.Response(404, json={})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = FandangoApiClient(http_client=http_client)
    report = drift_check(client, max_dates=1)

    assert report["ok"] is True
    assert report["calendar_date_count"] == 3
    assert report["inspected_dates"] == ["2026-04-28"]
    assert report["formats_by_date"] == {
        "2026-04-28": ["IMAX", "3D", "IMAX 70MM", "MYSTERY FORMAT"]
    }
    assert report["showtime_count_by_date"] == {"2026-04-28": 7}
    assert report["buyable_count_by_date"] == {"2026-04-28": 5}
    assert report["format_names_seen"] == [
        "3D",
        "IMAX",
        "IMAX 70MM",
        "MYSTERY FORMAT",
        "STANDARD",
    ]

    http_client.close()

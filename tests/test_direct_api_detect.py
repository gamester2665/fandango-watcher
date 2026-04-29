"""Tests for direct API detection adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fandango_watcher.config import (
    DirectApiConfig,
    FormatsConfig,
    MovieConfig,
    NotifyConfig,
    PollConfig,
    PurchaseConfig,
    TargetConfig,
    TheaterConfig,
    WatcherConfig,
)
from fandango_watcher.direct_api_detect import detect_target_direct_api
from fandango_watcher.models import FormatTag, ReleaseSchema

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fandango_api"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class _FakeClient:
    theater_id = "AAAWX"
    chain_code = "AMC"

    def calendar_dates(self) -> list[str]:
        return ["2026-04-28", "2026-04-29", "2026-07-17"]

    def showtimes_url(self, start_date: str) -> str:
        return f"https://example.test/{start_date}"

    def get_json(self, url: str) -> dict[str, Any]:
        return _fixture("showtimes_citywalk_mixed.json")


def _cfg() -> WatcherConfig:
    return WatcherConfig(
        targets=[
            TargetConfig(
                name="michael-imax",
                url="https://www.fandango.com/michael-2026/movie-overview",
            )
        ],
        theater=TheaterConfig(
            display_name="AMC Universal CityWalk",
            fandango_theater_anchor="AMC Universal CityWalk",
        ),
        formats=FormatsConfig(require=[FormatTag.IMAX], include=[]),
        direct_api=DirectApiConfig(max_dates_per_tick=2),
        poll=PollConfig(min_seconds=30, max_seconds=30),
        purchase=PurchaseConfig(enabled=False),
        notify=NotifyConfig(channels=[], on_events=[]),
        movies=[
            MovieConfig(
                key="michael",
                title="Michael (2026)",
                poster_url="https://www.fandango.com/michael-poster.jpg",
                fandango_targets=["michael-imax"],
                preferred_formats=[FormatTag.IMAX],
            )
        ],
    )


def test_direct_api_adapter_returns_parsed_page_data_for_matching_buyable_records() -> None:
    cfg = _cfg()
    result = detect_target_direct_api(
        cfg.targets[0],
        cfg,
        client=_FakeClient(),  # type: ignore[arg-type]
    )

    assert result.parsed.release_schema == ReleaseSchema.PARTIAL_RELEASE
    assert result.parsed.citywalk_present is True
    assert result.parsed.citywalk_showtime_count == 1
    assert result.parsed.ticket_url is not None
    assert result.parsed.poster_url == "https://www.fandango.com/michael-poster.jpg"
    assert result.meta.inspected_dates == ["2026-04-28"]
    assert result.meta.formats_seen == ["IMAX", "3D", "IMAX 70MM", "MYSTERY FORMAT"]
    assert result.meta.unknown_formats == ["MYSTERY FORMAT"]


def test_direct_api_adapter_returns_not_on_sale_when_movie_filter_misses() -> None:
    cfg = _cfg()
    cfg.movies[0].title = "Not In Fixture"

    result = detect_target_direct_api(
        cfg.targets[0],
        cfg,
        client=_FakeClient(),  # type: ignore[arg-type]
    )

    assert result.parsed.release_schema == ReleaseSchema.NOT_ON_SALE
    assert result.parsed.showtime_count == 0
    assert result.meta.inspected_dates == ["2026-04-28", "2026-04-29"]

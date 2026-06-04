"""Tests for release-date scan helpers."""

from __future__ import annotations

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
from fandango_watcher.detect import prefer_stronger_parsed
from fandango_watcher.models import NotOnSalePageData, ReleaseSchema, ShowtimesDisclosedPageData
from fandango_watcher.showtime_dates import (
    OVERVIEW_BROWSER_CONFIRM_DAYS_BEFORE,
    days_until_release,
    effective_crawl_url,
    merge_scan_dates,
    parse_format_from_target_url,
    parse_release_date_iso,
    priority_showtime_dates,
    should_browser_confirm_overview,
    target_is_format_filtered,
    target_uses_any_format_for_disclosed,
)


def test_parse_release_date_iso_opens_month_day() -> None:
    assert parse_release_date_iso("Opens Jul 17") == "2026-07-17"


def test_merge_scan_dates_puts_release_date_first() -> None:
    calendar = [f"2026-06-{day:02d}" for day in range(1, 31)]
    merged = merge_scan_dates(calendar, ["2026-07-17"], max_dates=5)
    assert merged[0] == "2026-07-17"
    assert "2026-07-17" in merged
    assert len(merged) == 5


def test_priority_showtime_dates_from_movie_and_text() -> None:
    cfg = WatcherConfig(
        targets=[
            TargetConfig(
                name="odyssey-overview",
                url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
            )
        ],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="CW"),
        formats=FormatsConfig(require=[], include=[]),
        poll=PollConfig(min_seconds=30, max_seconds=30),
        purchase=PurchaseConfig(enabled=False),
        notify=NotifyConfig(channels=[], on_events=[]),
        movies=[
            MovieConfig(
                key="odyssey",
                title="The Odyssey (2026)",
                release_date="2026-07-17",
                fandango_targets=["odyssey-overview"],
            )
        ],
    )
    dates = priority_showtime_dates(
        cfg.targets[0],
        cfg,
        release_date_text="Opens Jul 17",
    )
    assert dates == ["2026-07-17"]


def test_effective_crawl_url_appends_opening_date() -> None:
    cfg = WatcherConfig(
        targets=[
            TargetConfig(
                name="odyssey-overview",
                url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
            )
        ],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="CW"),
        formats=FormatsConfig(require=[], include=[]),
        poll=PollConfig(min_seconds=30, max_seconds=30),
        purchase=PurchaseConfig(enabled=False),
        notify=NotifyConfig(channels=[], on_events=[]),
        movies=[
            MovieConfig(
                key="odyssey",
                title="The Odyssey (2026)",
                release_date="2026-07-17",
                fandango_targets=["odyssey-overview"],
            )
        ],
    )
    url = effective_crawl_url(
        cfg.targets[0],
        cfg,
        release_date_text="Opens Jul 17",
    )
    assert "date=2026-07-17" in url


def test_target_uses_any_format_for_disclosed_overview_only() -> None:
    overview = TargetConfig(
        name="odyssey-overview",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
    )
    imax = TargetConfig(
        name="odyssey-imax-70mm",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview?format=IMAX%2070MM",
    )
    assert target_uses_any_format_for_disclosed(overview) is True
    assert target_uses_any_format_for_disclosed(imax) is False


def test_parse_format_from_target_url() -> None:
    url = "https://www.fandango.com/foo/movie-overview?format=IMAX%2070MM"
    assert parse_format_from_target_url(url) == "IMAX 70MM"


def test_target_is_format_filtered() -> None:
    overview = TargetConfig(
        name="odyssey-overview",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
    )
    imax = TargetConfig(
        name="odyssey-imax-70mm",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview?format=IMAX%2070MM",
    )
    assert target_is_format_filtered(overview) is False
    assert target_is_format_filtered(imax) is True


def test_should_browser_confirm_overview_throttled_far_from_release() -> None:
    from datetime import date

    cfg = WatcherConfig(
        targets=[
            TargetConfig(
                name="odyssey-overview",
                url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
            )
        ],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="CW"),
        formats=FormatsConfig(require=[], include=[]),
        poll=PollConfig(min_seconds=30, max_seconds=30),
        purchase=PurchaseConfig(enabled=False),
        notify=NotifyConfig(channels=[], on_events=[]),
        movies=[
            MovieConfig(
                key="odyssey",
                title="The Odyssey (2026)",
                release_date="2099-01-01",
                fandango_targets=["odyssey-overview"],
            )
        ],
    )
    parsed = NotOnSalePageData(
        url=cfg.targets[0].url,
        page_title="Future",
        theater_count=0,
        showtime_count=0,
    )
    assert (
        should_browser_confirm_overview(
            cfg.targets[0],
            cfg,
            parsed,
            release_date_text=None,
        )
        is False
    )
    assert days_until_release("2099-01-01", today=date(2026, 6, 4)) > OVERVIEW_BROWSER_CONFIRM_DAYS_BEFORE


def test_should_browser_confirm_overview_when_opening_day_known() -> None:
    from datetime import date, timedelta

    soon = (date.today() + timedelta(days=7)).isoformat()
    cfg = WatcherConfig(
        targets=[
            TargetConfig(
                name="odyssey-overview",
                url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
            )
        ],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="CW"),
        formats=FormatsConfig(require=[], include=[]),
        poll=PollConfig(min_seconds=30, max_seconds=30),
        purchase=PurchaseConfig(enabled=False),
        notify=NotifyConfig(channels=[], on_events=[]),
        movies=[
            MovieConfig(
                key="odyssey",
                title="The Odyssey (2026)",
                fandango_movie_id=241283,
                release_date=soon,
                fandango_targets=["odyssey-overview"],
            )
        ],
    )
    target = cfg.targets[0]
    parsed = NotOnSalePageData(
        url=target.url,
        page_title="The Odyssey (2026)",
        theater_count=0,
        showtime_count=0,
    )
    assert (
        should_browser_confirm_overview(
            target,
            cfg,
            parsed,
            release_date_text="Opens Jul 17",
        )
        is True
    )


def test_prefer_stronger_parsed_promotes_disclosed() -> None:
    api = NotOnSalePageData(
        url="https://example.com/movie-overview",
        page_title="Example",
        theater_count=0,
        showtime_count=0,
    )
    browser = ShowtimesDisclosedPageData(
        url="https://example.com/movie-overview",
        page_title="Example",
        showtime_count=12,
        buyable_showtime_count=0,
        theater_count=3,
        buyable_theater_count=0,
    )
    merged = prefer_stronger_parsed(api, browser)
    assert merged.release_schema == ReleaseSchema.SHOWTIMES_DISCLOSED
    assert merged.showtime_count == 12
    assert any("browser_overview_confirm" in e for e in merged.schema_evidence)

"""Tests for src/fandango_watcher/detect.py.

Covers:

* format-label normalization (priority: IMAX 70mm > IMAX > 70mm > Dolby > …)
* Schema A (not_on_sale) classification from an empty snapshot
* Schema B (partial_release) classification with a single CityWalk theater
* Schema C (full_release) classification once thresholds are crossed
* CityWalk anchor matching (case-insensitive substring)
* schema_evidence population
* ticket_url fallback to first showtime URL when no top-level link exists
"""

from __future__ import annotations

import pytest

from fandango_watcher.detect import (
    FULL_RELEASE_MIN_SHOWTIMES,
    FULL_RELEASE_MIN_THEATERS,
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
    normalize_format_label,
)
from fandango_watcher.models import (
    FormatTag,
    FullReleasePageData,
    NotOnSalePageData,
    PartialReleasePageData,
    ReleaseSchema,
    WatchStatus,
)

CITYWALK_ANCHOR = "AMC Universal CityWalk"


# -----------------------------------------------------------------------------
# Format label normalization
# -----------------------------------------------------------------------------


class TestNormalizeFormatLabel:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("IMAX 70MM", FormatTag.IMAX_70MM),
            ("imax 70mm", FormatTag.IMAX_70MM),
            ("IMAX-70MM", FormatTag.IMAX_70MM),
            ("IMAX 70 MM", FormatTag.IMAX_70MM),
            ("IMAX", FormatTag.IMAX),
            ("IMAX Digital", FormatTag.IMAX),
            ("70MM", FormatTag.SEVENTY_MM),
            ("70 MM", FormatTag.SEVENTY_MM),
            ("Dolby Cinema at AMC", FormatTag.DOLBY),
            ("Prime at AMC", FormatTag.DOLBY),
            ("Laser at AMC Recliners", FormatTag.LASER_RECLINER),
            ("Laser Recliner", FormatTag.LASER_RECLINER),
            ("Standard", FormatTag.STANDARD),
            ("Digital", FormatTag.STANDARD),
            ("4DX", FormatTag.OTHER),
            ("", FormatTag.OTHER),
        ],
    )
    def test_cases(self, label: str, expected: FormatTag) -> None:
        assert normalize_format_label(label) is expected

    def test_imax_70mm_beats_plain_imax(self) -> None:
        """Combined formats must win over individual components."""
        assert normalize_format_label("IMAX with 70mm film") is FormatTag.IMAX_70MM

    def test_laser_recliner_beats_recliner_alone(self) -> None:
        # "Recliner" alone shouldn't map to LASER_RECLINER.
        assert normalize_format_label("Recliner Seats") is FormatTag.OTHER


# -----------------------------------------------------------------------------
# Snapshot fixtures
# -----------------------------------------------------------------------------


def _empty_snapshot(**kwargs: object) -> PageSnapshot:
    defaults: dict[str, object] = {
        "url": "https://www.fandango.com/the-mandalorian-and-grogu-2026-242515/movie-overview",
        "page_title": "The Mandalorian and Grogu",
    }
    defaults.update(kwargs)
    return PageSnapshot(**defaults)  # type: ignore[arg-type]


def _citywalk_theater(
    *,
    sections: list[ExtractedFormatSection],
    name: str = "AMC Universal CityWalk 19 + IMAX",
) -> ExtractedTheater:
    return ExtractedTheater(name=name, format_sections=sections)


def _imax_70mm_section(
    *,
    n_showtimes: int = 2,
    with_urls: bool = True,
) -> ExtractedFormatSection:
    showtimes = [
        ExtractedShowtime(
            label=f"{7 + i}:00p",
            ticket_url=(
                f"https://www.fandango.com/ticketing/slot-{i}"
                if with_urls
                else None
            ),
        )
        for i in range(n_showtimes)
    ]
    return ExtractedFormatSection(label="IMAX 70MM", showtimes=showtimes)


# -----------------------------------------------------------------------------
# Schema A: not_on_sale
# -----------------------------------------------------------------------------


class TestNotOnSaleClassification:
    def test_empty_snapshot_is_not_on_sale(self) -> None:
        result = classify(_empty_snapshot(), citywalk_anchor=CITYWALK_ANCHOR)
        assert isinstance(result, NotOnSalePageData)
        assert result.release_schema == ReleaseSchema.NOT_ON_SALE
        assert result.watch_status == WatchStatus.NOT_WATCHABLE
        assert result.theater_count == 0
        assert result.showtime_count == 0
        assert result.citywalk_present is False

    def test_fanalert_flag_is_passed_through(self) -> None:
        result = classify(
            _empty_snapshot(fanalert_present=True, notify_me_present=True),
            citywalk_anchor=CITYWALK_ANCHOR,
        )
        assert result.fanalert_present is True
        assert result.notify_me_present is True
        assert "fanalert_present" in result.schema_evidence

    def test_loading_state_shows_up_in_evidence(self) -> None:
        result = classify(
            _empty_snapshot(
                loading_calendar_present=True,
                loading_format_filters_present=True,
            ),
            citywalk_anchor=CITYWALK_ANCHOR,
        )
        assert "loading_calendar_present" in result.schema_evidence
        assert "loading_format_filters_present" in result.schema_evidence


# -----------------------------------------------------------------------------
# Schema B: partial_release
# -----------------------------------------------------------------------------


class TestPartialReleaseClassification:
    def test_single_citywalk_imax_70mm_drop_is_partial(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[_citywalk_theater(sections=[_imax_70mm_section()])],
            format_filter_labels=["IMAX 70MM", "IMAX"],
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert isinstance(result, PartialReleasePageData)
        assert result.release_schema == ReleaseSchema.PARTIAL_RELEASE
        assert result.watch_status == WatchStatus.WATCHABLE
        assert result.theater_count == 1
        assert result.showtime_count == 2
        assert result.citywalk_present is True
        assert result.citywalk_showtime_count == 2
        assert result.formats_seen == ["IMAX_70MM"]
        assert result.citywalk_formats_seen == ["IMAX_70MM"]

    def test_format_filter_labels_are_normalized(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[_citywalk_theater(sections=[_imax_70mm_section()])],
            format_filter_labels=["IMAX 70MM", "IMAX", "Dolby Cinema at AMC"],
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        normalized = [ff.normalized_format for ff in result.format_filters_present]
        assert "IMAX_70MM" in normalized
        assert "IMAX" in normalized
        assert "DOLBY" in normalized

    def test_non_citywalk_theater_does_not_mark_citywalk_present(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[
                ExtractedTheater(
                    name="AMC Burbank 16",
                    format_sections=[_imax_70mm_section()],
                )
            ],
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert isinstance(result, PartialReleasePageData)
        assert result.citywalk_present is False
        assert result.citywalk_showtime_count == 0
        assert result.formats_seen == ["IMAX_70MM"]

    def test_ticket_url_falls_back_to_first_showtime_link(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[_citywalk_theater(sections=[_imax_70mm_section(with_urls=True)])],
            ticket_url=None,  # no top-level CTA
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert result.ticket_url == "https://www.fandango.com/ticketing/slot-0"

    def test_explicit_ticket_url_wins_over_fallback(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[_citywalk_theater(sections=[_imax_70mm_section(with_urls=True)])],
            ticket_url="https://www.fandango.com/explicit-cta",
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert result.ticket_url == "https://www.fandango.com/explicit-cta"


# -----------------------------------------------------------------------------
# Schema C: full_release
# -----------------------------------------------------------------------------


class TestFullReleaseClassification:
    def test_many_theaters_trips_full_release(self) -> None:
        """FULL_RELEASE_MIN_THEATERS or more theaters => full_release."""
        theaters = [
            ExtractedTheater(
                name=f"AMC Somewhere {i}",
                format_sections=[
                    ExtractedFormatSection(
                        label="Standard",
                        showtimes=[ExtractedShowtime(label="7:00p")],
                    )
                ],
            )
            for i in range(FULL_RELEASE_MIN_THEATERS)
        ]
        snapshot = _empty_snapshot(theaters=theaters)
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert isinstance(result, FullReleasePageData)
        assert result.release_schema == ReleaseSchema.FULL_RELEASE

    def test_many_showtimes_trips_full_release(self) -> None:
        """High showtime density alone also qualifies as full_release."""
        big_section = ExtractedFormatSection(
            label="Standard",
            showtimes=[
                ExtractedShowtime(label=f"{6 + i % 6}:00p")
                for i in range(FULL_RELEASE_MIN_SHOWTIMES)
            ],
        )
        snapshot = _empty_snapshot(
            theaters=[
                ExtractedTheater(
                    name="AMC Universal CityWalk 19 + IMAX",
                    format_sections=[big_section],
                )
            ]
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert isinstance(result, FullReleasePageData)
        assert result.showtime_count == FULL_RELEASE_MIN_SHOWTIMES


# -----------------------------------------------------------------------------
# CityWalk detection
# -----------------------------------------------------------------------------


class TestCityWalkDetection:
    @pytest.mark.parametrize(
        "theater_name,is_citywalk",
        [
            ("AMC Universal CityWalk 19 + IMAX", True),
            ("amc universal citywalk", True),
            ("AMC  Universal  CityWalk  19", True),  # extra spaces
            ("AMC CityWalk Orlando", False),  # no "Universal"
            ("AMC Burbank 16", False),
            ("Regal LA Live", False),
        ],
    )
    def test_anchor_matching(self, theater_name: str, is_citywalk: bool) -> None:
        snapshot = _empty_snapshot(
            theaters=[
                ExtractedTheater(
                    name=theater_name,
                    format_sections=[_imax_70mm_section()],
                )
            ]
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert result.citywalk_present is is_citywalk
        if is_citywalk:
            assert result.citywalk_showtime_count == 2
        else:
            assert result.citywalk_showtime_count == 0

    def test_theater_listings_carry_is_citywalk_flag(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[
                _citywalk_theater(sections=[_imax_70mm_section()]),
                ExtractedTheater(
                    name="AMC Burbank 16",
                    format_sections=[_imax_70mm_section(n_showtimes=1)],
                ),
            ]
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        by_name = {t.name: t for t in result.theaters}
        assert by_name["AMC Universal CityWalk 19 + IMAX"].is_citywalk is True
        assert by_name["AMC Burbank 16"].is_citywalk is False


# -----------------------------------------------------------------------------
# Evidence and debug fields
# -----------------------------------------------------------------------------


class TestSchemaEvidence:
    def test_includes_counts(self) -> None:
        snapshot = _empty_snapshot(
            theaters=[_citywalk_theater(sections=[_imax_70mm_section()])]
        )
        result = classify(snapshot, citywalk_anchor=CITYWALK_ANCHOR)
        assert "theater_count=1" in result.schema_evidence
        assert "showtime_count=2" in result.schema_evidence
        assert any("citywalk_showtime_count" in e for e in result.schema_evidence)

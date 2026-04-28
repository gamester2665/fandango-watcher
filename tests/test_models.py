# mypy: disable-error-code=arg-type
"""Tests for src/fandango_watcher/models.py.

These tests lock in the behavior of the Pydantic schemas that the rest of the
watcher depends on:

* the discriminated-union over ``release_schema``
* schema-specific validators (not_on_sale vs partial_release vs full_release)
* cross-field invariants on CityWalk counts
* deduplication in list fields
* the new FormatTag entries (DOLBY, LASER_RECLINER) used by config.example.yaml
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fandango_watcher.models import (
    CrawlContext,
    FormatFilter,
    FormatSection,
    FormatTag,
    FullReleasePageData,
    NotOnSalePageData,
    ParsedCounts,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
    WatchStatus,
    validate_page_data,
)

# ---------------------------------------------------------------------------
# FormatTag enum
# ---------------------------------------------------------------------------


class TestFormatTag:
    def test_all_expected_tags_present(self) -> None:
        values = {t.value for t in FormatTag}
        assert values == {
            "IMAX",
            "IMAX_70MM",
            "THREE_D",
            "SEVENTY_MM",
            "DOLBY",
            "LASER_RECLINER",
            "STANDARD",
            "OTHER",
        }

    def test_dolby_and_laser_recliner_added(self) -> None:
        """These were added in the Docker scaffolding turn for config.example.yaml."""
        assert FormatTag.DOLBY.value == "DOLBY"
        assert FormatTag.LASER_RECLINER.value == "LASER_RECLINER"

    def test_string_enum_behaviour(self) -> None:
        assert FormatTag.IMAX_70MM == "IMAX_70MM"
        assert str(FormatTag.IMAX) == "IMAX"


# ---------------------------------------------------------------------------
# Simple submodels
# ---------------------------------------------------------------------------


class TestSubmodels:
    def test_format_filter_defaults(self) -> None:
        ff = FormatFilter(label="IMAX 70MM")
        assert ff.normalized_format == FormatTag.OTHER
        assert ff.selected is False

    def test_showtime_defaults_buyable(self) -> None:
        s = Showtime(label="7:00p")
        assert s.is_buyable is True
        assert s.is_citywalk is False
        assert s.ticket_url is None

    def test_format_section_dedupes_attributes(self) -> None:
        fs = FormatSection(
            label="IMAX 70MM",
            attributes=["reserved", "imax", "reserved", "imax", "70mm"],
        )
        assert fs.attributes == ["reserved", "imax", "70mm"]

    def test_theater_listing_negative_distance_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TheaterListing(name="AMC", distance_miles=-1.0)

    def test_extra_fields_forbidden(self) -> None:
        """ModelBase sets extra='forbid' — stray fields must error loudly."""
        with pytest.raises(ValidationError):
            Showtime(label="7:00p", unknown_field=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ParsedCounts
# ---------------------------------------------------------------------------


class TestParsedCounts:
    def test_dedupes_formats_seen(self) -> None:
        pc = ParsedCounts(
            theater_count=1,
            showtime_count=2,
            formats_seen=[FormatTag.IMAX, FormatTag.IMAX, FormatTag.IMAX_70MM],
        )
        # use_enum_values=True means the value is the string after dump/validate.
        assert pc.formats_seen == ["IMAX", "IMAX_70MM"]

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParsedCounts(theater_count=-1, showtime_count=0)
        with pytest.raises(ValidationError):
            ParsedCounts(theater_count=0, showtime_count=-1)


# ---------------------------------------------------------------------------
# CrawlContext
# ---------------------------------------------------------------------------


class TestCrawlContext:
    def test_minimal_construction(self) -> None:
        ctx = CrawlContext(url="https://fandango.com/x", page_title="The Odyssey")
        assert ctx.movie_title is None
        assert ctx.fanalert_present is False
        assert ctx.format_filters_present == []

    def test_schema_evidence_dedupes(self) -> None:
        ctx = CrawlContext(
            url="https://fandango.com/x",
            page_title="The Odyssey",
            schema_evidence=["citywalk_card", "format_filter_imax", "citywalk_card"],
        )
        assert ctx.schema_evidence == ["citywalk_card", "format_filter_imax"]


# ---------------------------------------------------------------------------
# Schema-specific page models + discriminated union
# ---------------------------------------------------------------------------


def _base_fields(**extras: object) -> dict[str, object]:
    """Minimum required fields for any PageDataBase subclass."""
    base: dict[str, object] = {
        "url": "https://fandango.com/x",
        "page_title": "The Odyssey (2026)",
    }
    base.update(extras)
    return base


class TestNotOnSalePageData:
    def test_happy_path(self) -> None:
        page = NotOnSalePageData(
            **_base_fields(
                theater_count=0,
                showtime_count=0,
                fanalert_present=True,
            )
        )
        assert page.release_schema == "not_on_sale"
        assert page.watch_status == "not_watchable"

    def test_allows_theater_shells_without_showtimes(self) -> None:
        page = NotOnSalePageData(
            **_base_fields(theater_count=4, showtime_count=0)
        )
        assert page.release_schema == "not_on_sale"
        assert page.watch_status == "not_watchable"

    def test_rejects_populated_showtimes(self) -> None:
        with pytest.raises(ValidationError, match="real showtimes"):
            NotOnSalePageData(
                **_base_fields(theater_count=0, showtime_count=3)
            )

    def test_rejects_citywalk_showtimes(self) -> None:
        # citywalk_showtime_count must be 0 on a not_on_sale page. Set
        # citywalk_present=False so the "requires at least one showtime"
        # invariant doesn't fire first.
        with pytest.raises(ValidationError):
            NotOnSalePageData(
                **_base_fields(
                    theater_count=0,
                    showtime_count=0,
                    citywalk_present=False,
                    citywalk_showtime_count=1,
                )
            )


class TestPartialReleasePageData:
    def test_happy_path(self) -> None:
        page = PartialReleasePageData(
            **_base_fields(
                theater_count=1,
                showtime_count=2,
                formats_seen=[FormatTag.IMAX_70MM],
                citywalk_present=True,
                citywalk_showtime_count=2,
            )
        )
        assert page.release_schema == "partial_release"
        assert page.watch_status == "watchable"
        assert page.formats_seen == ["IMAX_70MM"]

    def test_requires_at_least_one_theater(self) -> None:
        with pytest.raises(ValidationError, match="at least one theater"):
            PartialReleasePageData(
                **_base_fields(theater_count=0, showtime_count=1)
            )

    def test_requires_at_least_one_showtime(self) -> None:
        with pytest.raises(ValidationError, match="at least one showtime"):
            PartialReleasePageData(
                **_base_fields(theater_count=1, showtime_count=0)
            )


class TestFullReleasePageData:
    def test_happy_path(self) -> None:
        page = FullReleasePageData(
            **_base_fields(
                theater_count=12,
                showtime_count=88,
                formats_seen=[FormatTag.IMAX, FormatTag.STANDARD],
            )
        )
        assert page.release_schema == "full_release"
        assert page.watch_status == "watchable"


# ---------------------------------------------------------------------------
# CityWalk cross-field invariants
# ---------------------------------------------------------------------------


class TestCityWalkInvariants:
    def test_citywalk_showtime_count_cannot_exceed_showtime_count(self) -> None:
        with pytest.raises(ValidationError, match="cannot exceed"):
            PartialReleasePageData(
                **_base_fields(
                    theater_count=1,
                    showtime_count=2,
                    citywalk_present=True,
                    citywalk_showtime_count=5,
                )
            )

    def test_citywalk_present_requires_citywalk_showtimes(self) -> None:
        with pytest.raises(ValidationError, match="at least one CityWalk"):
            PartialReleasePageData(
                **_base_fields(
                    theater_count=1,
                    showtime_count=2,
                    citywalk_present=True,
                    citywalk_showtime_count=0,
                )
            )


# ---------------------------------------------------------------------------
# validate_page_data discriminator
# ---------------------------------------------------------------------------


class TestDiscriminatedUnion:
    def test_dispatches_to_not_on_sale(self) -> None:
        result = validate_page_data(
            {
                "url": "https://fandango.com/x",
                "page_title": "X",
                "release_schema": "not_on_sale",
                "theater_count": 0,
                "showtime_count": 0,
            }
        )
        assert isinstance(result, NotOnSalePageData)

    def test_dispatches_to_partial_release(self) -> None:
        result = validate_page_data(
            {
                "url": "https://fandango.com/x",
                "page_title": "X",
                "release_schema": "partial_release",
                "theater_count": 1,
                "showtime_count": 3,
                "formats_seen": ["IMAX_70MM"],
            }
        )
        assert isinstance(result, PartialReleasePageData)
        assert result.watch_status == WatchStatus.WATCHABLE

    def test_dispatches_to_full_release(self) -> None:
        result = validate_page_data(
            {
                "url": "https://fandango.com/x",
                "page_title": "X",
                "release_schema": "full_release",
                "theater_count": 20,
                "showtime_count": 120,
            }
        )
        assert isinstance(result, FullReleasePageData)

    def test_unknown_release_schema_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_page_data(
                {
                    "url": "https://fandango.com/x",
                    "page_title": "X",
                    "release_schema": "future_schema",
                    "theater_count": 0,
                    "showtime_count": 0,
                }
            )


# ---------------------------------------------------------------------------
# ReleaseSchema / WatchStatus enum basics
# ---------------------------------------------------------------------------


class TestSchemaEnums:
    def test_release_schema_values(self) -> None:
        assert {s.value for s in ReleaseSchema} == {
            "not_on_sale",
            "partial_release",
            "full_release",
        }

    def test_watch_status_values(self) -> None:
        assert {s.value for s in WatchStatus} == {
            "watchable",
            "not_watchable",
            "unknown",
        }

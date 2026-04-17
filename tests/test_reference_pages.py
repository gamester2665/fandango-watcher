"""Tests for src/fandango_watcher/reference_pages.py."""

from __future__ import annotations

from fandango_watcher.models import FormatTag, ReleaseSchema
from fandango_watcher.reference_pages import (
    REFERENCE_PAGE_KEYS,
    REFERENCE_PAGES,
    REFERENCE_PAGES_READONLY,
    ReferencePage,
    get_reference_page,
)


class TestReferencePages:
    def test_expected_reference_keys_are_present(self) -> None:
        assert REFERENCE_PAGE_KEYS == (
            "the_odyssey_imax_70mm",
            "dune_part_three_imax_70mm",
            "the_mandalorian_and_grogu",
            "project_hail_mary",
        )

    def test_all_reference_values_are_typed_models(self) -> None:
        assert all(isinstance(page, ReferencePage) for page in REFERENCE_PAGES.values())

    def test_expected_schema_mapping(self) -> None:
        assert get_reference_page("the_mandalorian_and_grogu").expected_schema == (
            ReleaseSchema.NOT_ON_SALE
        )
        assert get_reference_page("the_odyssey_imax_70mm").expected_schema == (
            ReleaseSchema.PARTIAL_RELEASE
        )
        assert get_reference_page("project_hail_mary").expected_schema == (
            ReleaseSchema.FULL_RELEASE
        )

    def test_citywalk_priority_reference_is_flagged(self) -> None:
        dune = get_reference_page("dune_part_three_imax_70mm")
        assert dune.citywalk_priority is True
        assert dune.focus_theater == "Universal Cinema AMC at CityWalk Hollywood"

    def test_imax_70mm_references_preserve_format_preferences(self) -> None:
        odyssey = get_reference_page("the_odyssey_imax_70mm")
        assert odyssey.preferred_formats[:2] == [FormatTag.IMAX_70MM, FormatTag.IMAX]

    def test_urls_look_like_fandango_movie_pages(self) -> None:
        for page in REFERENCE_PAGES.values():
            assert page.url.startswith("https://www.fandango.com/")
            assert "/movie-overview" in page.url

    def test_readonly_mapping_reflects_same_objects(self) -> None:
        odyssey = get_reference_page("the_odyssey_imax_70mm")
        assert REFERENCE_PAGES_READONLY["the_odyssey_imax_70mm"] is odyssey

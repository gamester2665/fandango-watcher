"""Typed reference Fandango pages used during development.

These fixtures preserve the example URLs we have been reasoning about while
designing the watcher. They are intentionally code-visible so they can be used
from tests, docs generation, and future CLI helpers without copy/pasting links
out of PLAN.md.
"""

from __future__ import annotations

from types import MappingProxyType

from pydantic import Field

from .models import FormatTag, ModelBase, ReleaseSchema


class ReferencePage(ModelBase):
    key: str
    label: str
    url: str
    expected_schema: ReleaseSchema
    notes: str
    preferred_formats: list[FormatTag] = Field(default_factory=list)
    focus_theater: str | None = None
    citywalk_priority: bool = False


REFERENCE_PAGES: dict[str, ReferencePage] = {
    "the_odyssey_imax_70mm": ReferencePage(
        key="the_odyssey_imax_70mm",
        label="The Odyssey IMAX 70MM",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview?format=IMAX%2070MM",
        expected_schema=ReleaseSchema.PARTIAL_RELEASE,
        notes=(
            "Early premium-format release example. Useful for Schema B / partial "
            "release detection where only a small subset of theaters and times "
            "exist well ahead of general release."
        ),
        preferred_formats=[FormatTag.IMAX_70MM, FormatTag.IMAX, FormatTag.SEVENTY_MM],
    ),
    "dune_part_three_imax_70mm": ReferencePage(
        key="dune_part_three_imax_70mm",
        label="Dune: Part Three IMAX 70MM",
        url="https://www.fandango.com/dune-part-three-2026-244800/movie-overview?format=IMAX%2070MM",
        expected_schema=ReleaseSchema.PARTIAL_RELEASE,
        notes=(
            "Another Schema B page. Includes Universal Cinema AMC at CityWalk "
            "Hollywood in the observed theater list, so it is a strong reference "
            "case for CityWalk-priority parsing."
        ),
        preferred_formats=[FormatTag.IMAX_70MM, FormatTag.IMAX, FormatTag.SEVENTY_MM],
        focus_theater="Universal Cinema AMC at CityWalk Hollywood",
        citywalk_priority=True,
    ),
    "the_mandalorian_and_grogu": ReferencePage(
        key="the_mandalorian_and_grogu",
        label="The Mandalorian and Grogu",
        url="https://www.fandango.com/the-mandalorian-and-grogu-2026-242515/movie-overview",
        expected_schema=ReleaseSchema.NOT_ON_SALE,
        notes=(
            "Schema A / not-on-sale reference. The movie-times area resolves to a "
            "FanAlert / Notify Me form instead of real theater and showtime rows."
        ),
    ),
    "project_hail_mary": ReferencePage(
        key="project_hail_mary",
        label="Project Hail Mary",
        url="https://www.fandango.com/project-hail-mary-2026-243816/movie-overview",
        expected_schema=ReleaseSchema.FULL_RELEASE,
        notes=(
            "Schema C / broad-release reference. Used to compare against partial "
            "premium-format pages because it has denser theater and showtime coverage."
        ),
        preferred_formats=[FormatTag.STANDARD, FormatTag.SEVENTY_MM],
    ),
}

REFERENCE_PAGE_KEYS = tuple(REFERENCE_PAGES)
REFERENCE_PAGES_READONLY = MappingProxyType(REFERENCE_PAGES)


def get_reference_page(key: str) -> ReferencePage:
    """Return a named development reference page."""
    return REFERENCE_PAGES[key]

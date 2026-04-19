"""Pure detection + classification logic.

The watcher extracts a ``PageSnapshot`` from Fandango's DOM (via Playwright)
and hands it to :func:`classify`, which returns a validated
``ParsedPageData`` discriminated union.

Keeping this module browser-free makes the Schema A/B/C decision trivially
testable against synthetic fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from pydantic import Field

from .models import (
    FormatFilter,
    FormatSection,
    FormatTag,
    FullReleasePageData,
    ModelBase,
    NotOnSalePageData,
    ParsedPageData,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
)

# -----------------------------------------------------------------------------
# Tunable thresholds. Partial vs full release is a judgement call; keep the
# heuristic simple and positive-evidence-only. Treat anything above *either*
# threshold as full_release.
# -----------------------------------------------------------------------------
FULL_RELEASE_MIN_THEATERS = 5
FULL_RELEASE_MIN_SHOWTIMES = 20


# -----------------------------------------------------------------------------
# Extraction-tier models. These are what the browser-side extractor produces.
# Kept intentionally loose (raw labels, no enum normalization) so the extractor
# can be dumb; normalization happens in ``classify``.
# -----------------------------------------------------------------------------


class ExtractedShowtime(ModelBase):
    label: str
    ticket_url: str | None = None
    is_buyable: bool = True
    date_label: str | None = None


class ExtractedFormatSection(ModelBase):
    label: str
    attributes: list[str] = Field(default_factory=list)
    showtimes: list[ExtractedShowtime] = Field(default_factory=list)


class ExtractedTheater(ModelBase):
    name: str
    address: str | None = None
    distance_miles: float | None = Field(default=None, ge=0)
    format_sections: list[ExtractedFormatSection] = Field(default_factory=list)


class PageSnapshot(ModelBase):
    """Everything the extractor captured about a single Fandango page."""

    url: str
    page_title: str
    movie_title: str | None = None
    screenshot_path: str | None = None
    format_filter_labels: list[str] = Field(default_factory=list)
    theaters: list[ExtractedTheater] = Field(default_factory=list)
    fanalert_present: bool = False
    notify_me_present: bool = False
    loading_calendar_present: bool = False
    loading_format_filters_present: bool = False
    ticket_url: str | None = None


# -----------------------------------------------------------------------------
# Format label normalization.
# -----------------------------------------------------------------------------


def normalize_format_label(label: str) -> FormatTag:
    """Map a free-form Fandango format label to a ``FormatTag``.

    Order matters: combined formats like ``"IMAX 70MM"`` must win over
    plain ``"IMAX"`` or plain ``"70MM"``.
    """
    norm = label.upper().replace("-", " ")
    # Collapse whitespace.
    norm = " ".join(norm.split())

    has_imax = "IMAX" in norm
    has_70mm = "70MM" in norm or "70 MM" in norm
    has_laser = "LASER" in norm
    has_recliner = "RECLINER" in norm
    has_dolby_or_prime = "DOLBY" in norm or "PRIME" in norm

    if has_imax and has_70mm:
        return FormatTag.IMAX_70MM
    if has_imax:
        return FormatTag.IMAX
    if has_70mm:
        return FormatTag.SEVENTY_MM
    if has_laser and has_recliner:
        return FormatTag.LASER_RECLINER
    if has_dolby_or_prime:
        return FormatTag.DOLBY
    if "STANDARD" in norm or "DIGITAL" in norm:
        return FormatTag.STANDARD
    return FormatTag.OTHER


T = TypeVar("T")


def _dedupe_preserve_order(values: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    out: list[T] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


# -----------------------------------------------------------------------------
# Classifier.
# -----------------------------------------------------------------------------


def _normalize_whitespace(s: str) -> str:
    return " ".join(s.split()).lower()


def _is_citywalk(theater_name: str, citywalk_anchor: str) -> bool:
    """True when ``theater_name`` is the configured CityWalk venue.

    Primary rule: case-insensitive substring match (anchor in theater name).

    Fandango often renders the venue as **Universal Cinema AMC at CityWalk
    Hollywood** while configs say **AMC Universal CityWalk** — same place,
    different word order, so a plain substring check fails. When both strings
    contain *citywalk*, *amc*, and *universal*, treat it as the same anchor.
    """
    tl = _normalize_whitespace(theater_name)
    al = _normalize_whitespace(citywalk_anchor)
    if al in tl:
        return True
    if (
        "citywalk" in tl
        and "citywalk" in al
        and "amc" in tl
        and "amc" in al
        and "universal" in tl
        and "universal" in al
    ):
        return True
    return False


def _theater_listings(
    snapshot: PageSnapshot, *, citywalk_anchor: str
) -> list[TheaterListing]:
    return [
        TheaterListing(
            name=theater.name,
            address=theater.address,
            distance_miles=theater.distance_miles,
            is_citywalk=_is_citywalk(theater.name, citywalk_anchor),
            format_sections=[
                FormatSection(
                    label=fs.label,
                    normalized_format=normalize_format_label(fs.label),
                    attributes=fs.attributes,
                    showtimes=[
                        Showtime(
                            label=s.label,
                            ticket_url=s.ticket_url,
                            is_buyable=s.is_buyable,
                            is_citywalk=_is_citywalk(theater.name, citywalk_anchor),
                            date_label=s.date_label,
                        )
                        for s in fs.showtimes
                    ],
                )
                for fs in theater.format_sections
            ],
        )
        for theater in snapshot.theaters
    ]


def _pick_release_schema(
    *,
    theater_count: int,
    showtime_count: int,
) -> ReleaseSchema:
    """Positive-evidence-only schema selector.

    We deliberately do NOT read ``fanalert_present`` or
    ``Know When Tickets Go On Sale`` copy here — those headings can appear
    on Schema B pages even when tickets are live. See PLAN.md for the rule.
    """
    if theater_count == 0 and showtime_count == 0:
        return ReleaseSchema.NOT_ON_SALE
    if (
        theater_count >= FULL_RELEASE_MIN_THEATERS
        or showtime_count >= FULL_RELEASE_MIN_SHOWTIMES
    ):
        return ReleaseSchema.FULL_RELEASE
    return ReleaseSchema.PARTIAL_RELEASE


def classify(
    snapshot: PageSnapshot,
    *,
    citywalk_anchor: str,
) -> ParsedPageData:
    """Turn a ``PageSnapshot`` into a validated ``ParsedPageData``.

    ``citywalk_anchor`` is a substring matched (case-insensitive) against
    each theater name — e.g. ``"AMC Universal CityWalk"``.
    """
    theater_count = len(snapshot.theaters)
    showtime_count = sum(
        len(fs.showtimes)
        for theater in snapshot.theaters
        for fs in theater.format_sections
    )

    all_sections = [
        fs for theater in snapshot.theaters for fs in theater.format_sections
    ]
    formats_seen = _dedupe_preserve_order(
        normalize_format_label(fs.label) for fs in all_sections
    )

    citywalk_theaters = [
        theater
        for theater in snapshot.theaters
        if _is_citywalk(theater.name, citywalk_anchor)
    ]
    citywalk_showtime_count = sum(
        len(fs.showtimes)
        for theater in citywalk_theaters
        for fs in theater.format_sections
    )
    citywalk_formats_seen = _dedupe_preserve_order(
        normalize_format_label(fs.label)
        for theater in citywalk_theaters
        for fs in theater.format_sections
    )
    # Model invariant: citywalk_present requires at least one CityWalk showtime.
    citywalk_present = bool(citywalk_theaters) and citywalk_showtime_count > 0

    format_filters = [
        FormatFilter(
            label=lbl,
            normalized_format=normalize_format_label(lbl),
        )
        for lbl in snapshot.format_filter_labels
    ]

    release_schema = _pick_release_schema(
        theater_count=theater_count,
        showtime_count=showtime_count,
    )

    evidence: list[str] = [
        f"theater_count={theater_count}",
        f"showtime_count={showtime_count}",
    ]
    if snapshot.fanalert_present:
        evidence.append("fanalert_present")
    if snapshot.loading_calendar_present:
        evidence.append("loading_calendar_present")
    if snapshot.loading_format_filters_present:
        evidence.append("loading_format_filters_present")
    if citywalk_present:
        evidence.append(f"citywalk_showtime_count={citywalk_showtime_count}")

    # Ticket URL falls back to the first buyable showtime's URL when the
    # extractor didn't surface an explicit top-level link.
    ticket_url = snapshot.ticket_url
    if ticket_url is None:
        for theater in snapshot.theaters:
            for fs in theater.format_sections:
                for s in fs.showtimes:
                    if s.ticket_url:
                        ticket_url = s.ticket_url
                        break
                if ticket_url:
                    break
            if ticket_url:
                break

    payload: dict[str, object] = {
        "release_schema": release_schema.value,
        "url": snapshot.url,
        "page_title": snapshot.page_title,
        "movie_title": snapshot.movie_title,
        "screenshot_path": snapshot.screenshot_path,
        "loading_calendar_present": snapshot.loading_calendar_present,
        "loading_format_filters_present": snapshot.loading_format_filters_present,
        "fanalert_present": snapshot.fanalert_present,
        "notify_me_present": snapshot.notify_me_present,
        "format_filters_present": [ff.model_dump() for ff in format_filters],
        "ticket_url": ticket_url,
        "schema_evidence": evidence,
        "theater_count": theater_count,
        "showtime_count": showtime_count,
        "formats_seen": formats_seen,
        "citywalk_present": citywalk_present,
        "citywalk_showtime_count": citywalk_showtime_count,
        "citywalk_formats_seen": citywalk_formats_seen,
        "theaters": [t.model_dump() for t in _theater_listings(
            snapshot, citywalk_anchor=citywalk_anchor
        )],
    }

    # Dispatch to the correct concrete model. We don't go through
    # ``validate_page_data`` because that adapter re-validates all three
    # union members; picking the concrete class by release_schema is clearer
    # and gives better error messages on field-mismatch bugs.
    if release_schema is ReleaseSchema.NOT_ON_SALE:
        return NotOnSalePageData.model_validate(payload)
    if release_schema is ReleaseSchema.PARTIAL_RELEASE:
        return PartialReleasePageData.model_validate(payload)
    return FullReleasePageData.model_validate(payload)

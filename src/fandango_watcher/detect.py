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

from .config import TargetConfig, WatcherConfig
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
    ShowtimesDisclosedPageData,
    TheaterListing,
)
from .showtime_dates import target_is_format_filtered

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
    release_date_text: str | None = None
    poster_url: str | None = None
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
    has_3d = "3D" in norm or "REALD 3D" in norm or "REALD3D" in norm
    has_laser = "LASER" in norm
    has_recliner = "RECLINER" in norm
    has_dolby_or_prime = "DOLBY" in norm or "PRIME" in norm

    if has_imax and has_70mm:
        return FormatTag.IMAX_70MM
    if has_imax:
        return FormatTag.IMAX
    if has_3d:
        return FormatTag.THREE_D
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
    buyable_theater_count: int,
    buyable_showtime_count: int,
) -> ReleaseSchema:
    """Positive-evidence-only schema selector.

    We deliberately do NOT read ``fanalert_present`` or
    ``Know When Tickets Go On Sale`` copy here — those headings can appear
    on Schema B pages even when tickets are live. See PLAN.md for the rule.

    Theater DOM nodes without any parsed showtime rows (format-filtered or
    slow-render pages) must not become ``partial_release`` — that schema
    requires at least one buyable showtime. Treat ``showtime_count == 0`` as
    ``not_on_sale`` even when ``theater_count > 0``.

    Visible but non-buyable showtimes (disclosure day / "Coming soon") map to
    ``showtimes_disclosed`` so alerts and purchase do not treat them as live.
    """
    if showtime_count == 0:
        return ReleaseSchema.NOT_ON_SALE
    if buyable_showtime_count == 0:
        return ReleaseSchema.SHOWTIMES_DISCLOSED
    if (
        buyable_theater_count >= FULL_RELEASE_MIN_THEATERS
        or buyable_showtime_count >= FULL_RELEASE_MIN_SHOWTIMES
    ):
        return ReleaseSchema.FULL_RELEASE
    return ReleaseSchema.PARTIAL_RELEASE


def _count_showtimes(
    theaters: list[TheaterListing],
) -> tuple[int, int, int]:
    """Return total showtimes, buyable showtimes, and buyable theater count."""
    showtime_count = 0
    buyable_showtime_count = 0
    buyable_theater_count = 0
    for theater in theaters:
        theater_buyable = 0
        for fs in theater.format_sections:
            for st in fs.showtimes:
                showtime_count += 1
                if st.is_buyable:
                    buyable_showtime_count += 1
                    theater_buyable += 1
        if theater_buyable > 0:
            buyable_theater_count += 1
    return showtime_count, buyable_showtime_count, buyable_theater_count


def classify(
    snapshot: PageSnapshot,
    *,
    citywalk_anchor: str,
) -> ParsedPageData:
    """Turn a ``PageSnapshot`` into a validated ``ParsedPageData``.

    ``citywalk_anchor`` is a substring matched (case-insensitive) against
    each theater name — e.g. ``"AMC Universal CityWalk"``.
    """
    theaters = _theater_listings(snapshot, citywalk_anchor=citywalk_anchor)
    theater_count = len(theaters)
    showtime_count, buyable_showtime_count, buyable_theater_count = _count_showtimes(
        theaters
    )

    all_sections = [
        fs for theater in theaters for fs in theater.format_sections
    ]
    formats_seen = _dedupe_preserve_order(
        normalize_format_label(fs.label) for fs in all_sections
    )

    citywalk_theaters = [theater for theater in theaters if theater.is_citywalk]
    citywalk_showtime_count = sum(
        len(fs.showtimes)
        for theater in citywalk_theaters
        for fs in theater.format_sections
    )
    buyable_citywalk_showtime_count = sum(
        1
        for theater in citywalk_theaters
        for fs in theater.format_sections
        for st in fs.showtimes
        if st.is_buyable
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
        buyable_theater_count=buyable_theater_count,
        buyable_showtime_count=buyable_showtime_count,
    )

    evidence: list[str] = [
        f"theater_count={theater_count}",
        f"showtime_count={showtime_count}",
        f"buyable_showtime_count={buyable_showtime_count}",
        f"buyable_theater_count={buyable_theater_count}",
    ]
    if snapshot.fanalert_present:
        evidence.append("fanalert_present")
    if snapshot.loading_calendar_present:
        evidence.append("loading_calendar_present")
    if snapshot.loading_format_filters_present:
        evidence.append("loading_format_filters_present")
    if citywalk_present:
        evidence.append(f"citywalk_showtime_count={citywalk_showtime_count}")
    elif showtime_count > 0:
        evidence.append("regional_showtimes_only")

    # Ticket URL falls back to the first buyable showtime's URL when the
    # extractor didn't surface an explicit top-level link.
    ticket_url = snapshot.ticket_url
    if ticket_url is None:
        for theater in theaters:
            for fs in theater.format_sections:
                for s in fs.showtimes:
                    if s.ticket_url and s.is_buyable:
                        ticket_url = s.ticket_url
                        break
                if ticket_url:
                    break
            if ticket_url:
                break

    if release_schema is ReleaseSchema.SHOWTIMES_DISCLOSED:
        ticket_url = None

    payload: dict[str, object] = {
        "release_schema": release_schema.value,
        "url": snapshot.url,
        "page_title": snapshot.page_title,
        "movie_title": snapshot.movie_title,
        "release_date_text": snapshot.release_date_text,
        "poster_url": snapshot.poster_url,
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
        "buyable_showtime_count": buyable_showtime_count,
        "buyable_theater_count": buyable_theater_count,
        "formats_seen": formats_seen,
        "citywalk_present": citywalk_present,
        "citywalk_showtime_count": citywalk_showtime_count,
        "buyable_citywalk_showtime_count": buyable_citywalk_showtime_count,
        "citywalk_formats_seen": citywalk_formats_seen,
        "theaters": [t.model_dump() for t in theaters],
    }

    if release_schema is ReleaseSchema.NOT_ON_SALE:
        return NotOnSalePageData.model_validate(payload)
    if release_schema is ReleaseSchema.SHOWTIMES_DISCLOSED:
        return ShowtimesDisclosedPageData.model_validate(payload)
    if release_schema is ReleaseSchema.PARTIAL_RELEASE:
        return PartialReleasePageData.model_validate(payload)
    return FullReleasePageData.model_validate(payload)


_SCHEMA_RANK: dict[str, int] = {
    ReleaseSchema.NOT_ON_SALE.value: 1,
    ReleaseSchema.SHOWTIMES_DISCLOSED.value: 2,
    ReleaseSchema.PARTIAL_RELEASE.value: 3,
    ReleaseSchema.FULL_RELEASE.value: 4,
}


def _schema_rank_value(schema: ReleaseSchema | str) -> int:
    value = schema.value if isinstance(schema, ReleaseSchema) else str(schema)
    return _SCHEMA_RANK.get(value, 0)


def _format_tag_value(tag: FormatTag | str) -> str:
    return tag.value if isinstance(tag, FormatTag) else str(tag)


def refine_parsed_for_target(
    parsed: ParsedPageData,
    target: TargetConfig,
    cfg: WatcherConfig,
    *,
    citywalk_anchor: str,
) -> ParsedPageData:
    """Drop format sections that do not match a format-filtered target URL/config."""
    from .direct_api_detect import wanted_formats_for_target

    if not target_is_format_filtered(target):
        return parsed
    wanted = wanted_formats_for_target(target, cfg)
    if not wanted:
        return parsed

    extracted: list[ExtractedTheater] = []
    for theater in parsed.theaters:
        sections: list[ExtractedFormatSection] = []
        for fs in theater.format_sections:
            tag = _format_tag_value(fs.normalized_format)
            label_tag = _format_tag_value(normalize_format_label(fs.label))
            if tag in wanted or label_tag in wanted:
                sections.append(
                    ExtractedFormatSection(
                        label=fs.label,
                        showtimes=[
                            ExtractedShowtime(
                                label=st.label,
                                ticket_url=st.ticket_url,
                                is_buyable=st.is_buyable,
                                date_label=st.date_label,
                            )
                            for st in fs.showtimes
                        ],
                    )
                )
        if sections:
            extracted.append(
                ExtractedTheater(
                    name=theater.name,
                    address=theater.address,
                    distance_miles=theater.distance_miles,
                    format_sections=sections,
                )
            )

    prior_evidence = list(parsed.schema_evidence)
    if not extracted:
        snapshot = PageSnapshot(
            url=parsed.url,
            page_title=parsed.page_title or "",
            movie_title=parsed.movie_title,
            release_date_text=parsed.release_date_text,
            poster_url=parsed.poster_url,
            screenshot_path=parsed.screenshot_path,
            theaters=[],
        )
        refined = classify(snapshot, citywalk_anchor=citywalk_anchor)
        tags = ",".join(sorted(wanted))
        return refined.model_copy(
            update={
                "schema_evidence": [
                    *prior_evidence,
                    f"format_filter={tags}",
                    "format_filter_no_matching_sections",
                ],
            }
        )

    snapshot = PageSnapshot(
        url=parsed.url,
        page_title=parsed.page_title or "",
        movie_title=parsed.movie_title,
        release_date_text=parsed.release_date_text,
        poster_url=parsed.poster_url,
        screenshot_path=parsed.screenshot_path,
        format_filter_labels=[
            ff.label for ff in (parsed.format_filters_present or [])
        ],
        theaters=extracted,
    )
    refined = classify(snapshot, citywalk_anchor=citywalk_anchor)
    tags = ",".join(sorted(wanted))
    return refined.model_copy(
        update={
            "schema_evidence": [
                *prior_evidence,
                f"format_filter={tags}",
                *refined.schema_evidence,
            ],
        }
    )


def prefer_stronger_parsed(
    primary: ParsedPageData,
    secondary: ParsedPageData,
) -> ParsedPageData:
    """Pick the parse with stronger on-sale evidence (direct API vs browser)."""
    primary_rank = _schema_rank_value(primary.release_schema)
    secondary_rank = _schema_rank_value(secondary.release_schema)
    primary_st = primary.showtime_count or 0
    secondary_st = secondary.showtime_count or 0
    pick_secondary = secondary_rank > primary_rank or (
        secondary_rank == primary_rank and secondary_st > primary_st
    )
    chosen = secondary if pick_secondary else primary
    other = primary if pick_secondary else secondary
    evidence = list(getattr(chosen, "schema_evidence", []) or [])
    for item in getattr(other, "schema_evidence", []) or []:
        tagged = f"alt_parse:{item}"
        if tagged not in evidence:
            evidence.append(tagged)
    evidence.append(
        "browser_overview_confirm"
        if pick_secondary
        else "browser_overview_confirm_kept_direct"
    )
    return chosen.model_copy(update={"schema_evidence": evidence})

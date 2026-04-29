from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class ReleaseSchema(StrEnum):
    NOT_ON_SALE = "not_on_sale"
    PARTIAL_RELEASE = "partial_release"
    FULL_RELEASE = "full_release"


class WatchStatus(StrEnum):
    WATCHABLE = "watchable"
    NOT_WATCHABLE = "not_watchable"
    UNKNOWN = "unknown"


class FormatTag(StrEnum):
    IMAX = "IMAX"
    IMAX_70MM = "IMAX_70MM"
    THREE_D = "THREE_D"
    SEVENTY_MM = "SEVENTY_MM"
    DOLBY = "DOLBY"
    LASER_RECLINER = "LASER_RECLINER"
    STANDARD = "STANDARD"
    OTHER = "OTHER"


class ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _dedupe_preserve_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    deduped: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


class FormatFilter(ModelBase):
    label: str
    normalized_format: FormatTag = FormatTag.OTHER
    selected: bool = False


class Showtime(ModelBase):
    label: str
    ticket_url: str | None = None
    is_buyable: bool = True
    is_citywalk: bool = False
    date_label: str | None = None


class FormatSection(ModelBase):
    label: str
    normalized_format: FormatTag = FormatTag.OTHER
    attributes: list[str] = Field(default_factory=list)
    showtimes: list[Showtime] = Field(default_factory=list)

    @field_validator("attributes")
    @classmethod
    def dedupe_attributes(cls, values: list[str]) -> list[str]:
        return _dedupe_preserve_order(values)


class TheaterListing(ModelBase):
    name: str
    address: str | None = None
    distance_miles: float | None = Field(default=None, ge=0)
    is_citywalk: bool = False
    format_sections: list[FormatSection] = Field(default_factory=list)


class CrawlContext(ModelBase):
    url: str
    page_title: str
    movie_title: str | None = None
    poster_url: str | None = None
    theater_zip: str | None = None
    crawled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    screenshot_path: str | None = None
    loading_calendar_present: bool = False
    loading_format_filters_present: bool = False
    fanalert_present: bool = False
    notify_me_present: bool = False
    format_filters_present: list[FormatFilter] = Field(default_factory=list)
    ticket_url: str | None = None
    schema_evidence: list[str] = Field(default_factory=list)

    @field_validator("schema_evidence")
    @classmethod
    def dedupe_schema_evidence(cls, values: list[str]) -> list[str]:
        return _dedupe_preserve_order(values)


class ParsedCounts(ModelBase):
    theater_count: int = Field(ge=0)
    showtime_count: int = Field(ge=0)
    formats_seen: list[FormatTag] = Field(default_factory=list)
    citywalk_present: bool = False
    citywalk_showtime_count: int = Field(default=0, ge=0)
    citywalk_formats_seen: list[FormatTag] = Field(default_factory=list)

    @field_validator("formats_seen", "citywalk_formats_seen")
    @classmethod
    def dedupe_formats(cls, values: list[FormatTag]) -> list[FormatTag]:
        return _dedupe_preserve_order(values)


class PageDataBase(CrawlContext, ParsedCounts):
    watch_status: WatchStatus = WatchStatus.UNKNOWN
    theaters: list[TheaterListing] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_citywalk_counts(self) -> PageDataBase:
        if self.citywalk_showtime_count > self.showtime_count:
            raise ValueError("citywalk_showtime_count cannot exceed showtime_count")
        if self.citywalk_present and self.citywalk_showtime_count == 0:
            raise ValueError("citywalk_present requires at least one CityWalk showtime")
        return self


class NotOnSalePageData(PageDataBase):
    release_schema: Literal[ReleaseSchema.NOT_ON_SALE] = ReleaseSchema.NOT_ON_SALE
    watch_status: Literal[WatchStatus.NOT_WATCHABLE] = WatchStatus.NOT_WATCHABLE

    @model_validator(mode="after")
    def validate_not_on_sale(self) -> NotOnSalePageData:
        # ``theater_count`` may be > 0 when the extractor sees theater shells
        # but no parseable showtime links (same positive-evidence rule as
        # :func:`~fandango_watcher.detect._pick_release_schema`).
        if self.showtime_count != 0:
            raise ValueError("not_on_sale pages should not include real showtimes")
        if self.citywalk_showtime_count != 0:
            raise ValueError("not_on_sale pages cannot include CityWalk showtimes")
        return self


class PartialReleasePageData(PageDataBase):
    release_schema: Literal[ReleaseSchema.PARTIAL_RELEASE] = ReleaseSchema.PARTIAL_RELEASE
    watch_status: Literal[WatchStatus.WATCHABLE] = WatchStatus.WATCHABLE

    @model_validator(mode="after")
    def validate_partial_release(self) -> PartialReleasePageData:
        if self.theater_count <= 0:
            raise ValueError("partial_release pages must include at least one theater")
        if self.showtime_count <= 0:
            raise ValueError("partial_release pages must include at least one showtime")
        return self


class FullReleasePageData(PageDataBase):
    release_schema: Literal[ReleaseSchema.FULL_RELEASE] = ReleaseSchema.FULL_RELEASE
    watch_status: Literal[WatchStatus.WATCHABLE] = WatchStatus.WATCHABLE

    @model_validator(mode="after")
    def validate_full_release(self) -> FullReleasePageData:
        if self.theater_count <= 0:
            raise ValueError("full_release pages must include at least one theater")
        if self.showtime_count <= 0:
            raise ValueError("full_release pages must include at least one showtime")
        return self


ParsedPageData = Annotated[
    NotOnSalePageData | PartialReleasePageData | FullReleasePageData,
    Field(discriminator="release_schema"),
]

PARSED_PAGE_DATA_ADAPTER: TypeAdapter[ParsedPageData] = TypeAdapter(ParsedPageData)


def validate_page_data(payload: dict[str, Any]) -> ParsedPageData:
    """Validate a parsed Fandango crawl payload."""
    return PARSED_PAGE_DATA_ADAPTER.validate_python(payload)


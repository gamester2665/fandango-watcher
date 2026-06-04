"""Release-date helpers for Fandango calendar / showtime scans."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .config import TargetConfig, WatcherConfig

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_OPENS_MONTH_DAY_RE = re.compile(
    r"\b(?:opens?|opening)\s+"
    r"(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})\b"
)
_TITLE_YEAR_RE = re.compile(r"\((\d{4})\)\s*$")
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _valid_iso_date(value: str) -> bool:
    match = _ISO_DATE_RE.match(value.strip())
    if not match:
        return False
    year, month, day = (int(match.group(i)) for i in range(1, 4))
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _month_number(name: str) -> int | None:
    return _MONTHS.get(name.strip().lower())


def _infer_year(
    month: int,
    day: int,
    *,
    hint_year: int | None,
    today: date | None = None,
) -> int:
    if hint_year is not None:
        return hint_year
    ref = today or datetime.now(UTC).date()
    candidate = date(ref.year, month, day)
    if candidate < ref:
        return ref.year + 1
    return ref.year


def parse_release_date_iso(text: str | None) -> str | None:
    """Return ``YYYY-MM-DD`` when ``text`` contains a parseable release date."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _valid_iso_date(raw):
        return raw
    year_match = _TITLE_YEAR_RE.search(raw)
    hint_year = int(year_match.group(1)) if year_match else None
    match = _MONTH_DAY_YEAR_RE.search(raw)
    if match:
        month = _month_number(match.group("month"))
        if month is not None:
            return date(
                int(match.group("year")),
                month,
                int(match.group("day")),
            ).isoformat()
    match = _OPENS_MONTH_DAY_RE.search(raw)
    if match:
        month = _month_number(match.group("month"))
        if month is not None:
            year = _infer_year(
                month,
                int(match.group("day")),
                hint_year=hint_year,
            )
            return date(year, month, int(match.group("day"))).isoformat()
    return None


def parse_date_from_target_url(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    for key in ("date", "startDate"):
        values = query.get(key) or []
        for value in values:
            candidate = value.strip()[:10]
            if _valid_iso_date(candidate):
                return candidate
    return None


def priority_showtime_dates(
    target: TargetConfig,
    cfg: WatcherConfig,
    *,
    release_date_text: str | None = None,
) -> list[str]:
    """Dates that must be scanned even if they fall outside the calendar prefix."""
    dates: list[str] = []
    movie = cfg.movie_for_target(target.name)
    if movie is not None:
        if movie.release_date:
            dates.append(movie.release_date)
        dates.append(parse_release_date_iso(movie.title) or "")
    dates.append(parse_date_from_target_url(target.url) or "")
    if release_date_text:
        dates.append(parse_release_date_iso(release_date_text) or "")
    out: list[str] = []
    seen: set[str] = set()
    for value in dates:
        if value and value not in seen and _valid_iso_date(value):
            out.append(value)
            seen.add(value)
    return out


def merge_scan_dates(
    calendar_dates: list[str],
    priority_dates: list[str],
    *,
    max_dates: int,
) -> list[str]:
    """Always scan ``priority_dates``; fill remaining budget from ``calendar_dates``."""
    limit = max(1, max_dates)
    ordered: list[str] = []
    seen: set[str] = set()
    for showtime_date in priority_dates:
        if showtime_date in seen:
            continue
        ordered.append(showtime_date)
        seen.add(showtime_date)
    for showtime_date in calendar_dates:
        if len(ordered) >= limit:
            break
        if showtime_date in seen:
            continue
        ordered.append(showtime_date)
        seen.add(showtime_date)
    return ordered


def target_uses_any_format_for_disclosed(target: TargetConfig) -> bool:
    """Overview movie pages list all formats; only format-specific URLs are filtered."""
    if "format=" in target.url.lower():
        return False
    return target.name.endswith("-overview") or "/movie-overview" in target.url.lower()


def effective_crawl_url(
    target: TargetConfig,
    cfg: WatcherConfig,
    *,
    release_date_text: str | None = None,
) -> str:
    """Append ``?date=`` when we know the opening day but the URL lacks it."""
    if parse_date_from_target_url(target.url):
        return target.url
    priority = priority_showtime_dates(
        target,
        cfg,
        release_date_text=release_date_text,
    )
    if not priority:
        return target.url
    showtime_date = priority[0]
    separator = "&" if "?" in target.url else "?"
    return f"{target.url}{separator}date={showtime_date}"

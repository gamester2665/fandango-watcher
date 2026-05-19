"""Shared watchlist build helpers for dashboard, Worker API, and seed CLI."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .config import MovieConfig, TargetConfig
from .models import FormatTag


def movie_key_from_title(title: str) -> str:
    base = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip() or title
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug.replace("-", "_")


def unique_name(base: str, existing: set[str], *, separator: str = "-") -> str:
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}{separator}{n}"
        n += 1
    existing.add(candidate)
    return candidate


def movie_id_from_url(url: str) -> int | None:
    match = re.search(r"-(\d+)/movie-overview(?:$|[/?#])", url)
    return int(match.group(1)) if match else None


def build_movie_add_plan(
    payload: dict[str, Any],
    *,
    existing_target_names: set[str],
    existing_movie_keys: set[str],
) -> tuple[MovieConfig, list[TargetConfig]]:
    """Build ``MovieConfig`` + ``TargetConfig`` rows from a Fandango search payload."""

    title = _first_nonempty_str(payload.get("title"))
    url = _first_nonempty_str(payload.get("url"))
    if not title or not url:
        raise ValueError("title and url are required")
    if not url.startswith("https://www.fandango.com/") or "/movie-overview" not in url:
        raise ValueError("url must be a Fandango movie-overview URL")

    overview_url = url.split("?", 1)[0]
    movie_id = payload.get("movie_id")
    if not isinstance(movie_id, int):
        movie_id = movie_id_from_url(overview_url)
    include_imax_70mm = bool(payload.get("include_imax_70mm", True))

    target_names = set(existing_target_names)
    movie_keys = set(existing_movie_keys)
    key = unique_name(movie_key_from_title(title), movie_keys, separator="_")
    prefix = key.replace("_", "-")

    new_targets: list[TargetConfig] = []
    overview_name = unique_name(f"{prefix}-overview", target_names)
    new_targets.append(
        TargetConfig(name=overview_name, url=overview_url),
    )
    if include_imax_70mm:
        imax_name = unique_name(f"{prefix}-imax-70mm", target_names)
        new_targets.append(
            TargetConfig(
                name=imax_name,
                url=f"{overview_url}?format={quote('IMAX 70MM')}",
            ),
        )

    preferred_formats: list[FormatTag] = (
        [FormatTag.IMAX_70MM, FormatTag.IMAX]
        if include_imax_70mm
        else [FormatTag.IMAX]
    )

    movie = MovieConfig(
        key=key,
        title=title,
        fandango_movie_id=movie_id,
        release_date=_first_nonempty_str(payload.get("release_date_text")),
        poster_url=_first_nonempty_str(payload.get("poster_url")),
        fandango_targets=[t.name for t in new_targets],
        preferred_formats=preferred_formats,
        x_handles=[],
    )
    return movie, new_targets


def _first_nonempty_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

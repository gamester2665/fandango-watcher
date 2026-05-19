"""Cloudflare D1 watchlist persistence (movies + targets + revision)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import MovieConfig, TargetConfig
from .models import FormatTag

logger = logging.getLogger(__name__)


class ConfigConflictError(Exception):
    """Raised when an optimistic revision check fails."""


class MoviePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    release_date: str | None = None
    poster_url: str | None = None
    preferred_formats: list[FormatTag] | None = None
    x_handles: list[str] | None = None
    x_keywords: list[str] | None = None
    distributor: str | None = None
    reference_page_key: str | None = None


INIT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS config_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS targets (
      name TEXT PRIMARY KEY,
      url TEXT NOT NULL,
      wait_until TEXT NOT NULL DEFAULT 'domcontentloaded',
      timeout_ms INTEGER NOT NULL DEFAULT 30000,
      format_filter_click_selector TEXT,
      format_filter_click_label TEXT,
      format_filter_click_timeout_ms INTEGER NOT NULL DEFAULT 12000,
      direct_api_movie_id INTEGER,
      direct_api_movie_title TEXT,
      direct_api_formats_json TEXT NOT NULL DEFAULT '[]',
      sort_order INTEGER NOT NULL DEFAULT 0
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS movies (
      key TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      fandango_movie_id INTEGER,
      distributor TEXT,
      release_date TEXT,
      poster_url TEXT,
      fandango_targets_json TEXT NOT NULL DEFAULT '[]',
      preferred_formats_json TEXT NOT NULL DEFAULT '[]',
      x_handles_json TEXT NOT NULL DEFAULT '[]',
      x_keywords_json TEXT NOT NULL DEFAULT '[]',
      reference_page_key TEXT,
      sort_order INTEGER NOT NULL DEFAULT 0
    )
    """.strip(),
)


def _loads_json_list(raw: str | None, *, field: str) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {field}: {raw!r}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{field} must be a JSON list, got {type(data).__name__}")
    return data


def target_row_to_model(row: dict[str, Any]) -> TargetConfig:
    formats_raw = _loads_json_list(row.get("direct_api_formats_json"), field="direct_api_formats_json")
    formats: list[FormatTag] = [FormatTag(v) for v in formats_raw]
    return TargetConfig(
        name=str(row["name"]),
        url=str(row["url"]),
        wait_until=row.get("wait_until") or "domcontentloaded",
        timeout_ms=int(row.get("timeout_ms") or 30000),
        format_filter_click_selector=row.get("format_filter_click_selector"),
        format_filter_click_label=row.get("format_filter_click_label"),
        format_filter_click_timeout_ms=int(row.get("format_filter_click_timeout_ms") or 12000),
        direct_api_movie_id=row.get("direct_api_movie_id"),
        direct_api_movie_title=row.get("direct_api_movie_title"),
        direct_api_formats=formats,
    )


def movie_row_to_model(row: dict[str, Any]) -> MovieConfig:
    fandango_targets = [str(v) for v in _loads_json_list(row.get("fandango_targets_json"), field="fandango_targets_json")]
    preferred_formats = [
        FormatTag(v)
        for v in _loads_json_list(row.get("preferred_formats_json"), field="preferred_formats_json")
    ]
    x_handles = [str(v) for v in _loads_json_list(row.get("x_handles_json"), field="x_handles_json")]
    x_keywords = [str(v) for v in _loads_json_list(row.get("x_keywords_json"), field="x_keywords_json")]
    return MovieConfig(
        key=str(row["key"]),
        title=str(row["title"]),
        fandango_movie_id=row.get("fandango_movie_id"),
        distributor=row.get("distributor"),
        release_date=row.get("release_date"),
        poster_url=row.get("poster_url"),
        fandango_targets=fandango_targets,
        preferred_formats=preferred_formats,
        x_handles=x_handles,
        x_keywords=x_keywords,
        reference_page_key=row.get("reference_page_key"),
    )


def target_model_to_row(target: TargetConfig, *, sort_order: int) -> dict[str, Any]:
    return {
        "name": target.name,
        "url": target.url,
        "wait_until": target.wait_until,
        "timeout_ms": target.timeout_ms,
        "format_filter_click_selector": target.format_filter_click_selector,
        "format_filter_click_label": target.format_filter_click_label,
        "format_filter_click_timeout_ms": target.format_filter_click_timeout_ms,
        "direct_api_movie_id": target.direct_api_movie_id,
        "direct_api_movie_title": target.direct_api_movie_title,
        "direct_api_formats_json": json.dumps(list(target.direct_api_formats)),
        "sort_order": sort_order,
    }


def movie_model_to_row(movie: MovieConfig, *, sort_order: int) -> dict[str, Any]:
    return {
        "key": movie.key,
        "title": movie.title,
        "fandango_movie_id": movie.fandango_movie_id,
        "distributor": movie.distributor,
        "release_date": movie.release_date,
        "poster_url": movie.poster_url,
        "fandango_targets_json": json.dumps(list(movie.fandango_targets)),
        "preferred_formats_json": json.dumps(list(movie.preferred_formats)),
        "x_handles_json": json.dumps(list(movie.x_handles)),
        "x_keywords_json": json.dumps(list(movie.x_keywords)),
        "reference_page_key": movie.reference_page_key,
        "sort_order": sort_order,
    }


class D1WatchlistProvider:
    """Cloudflare D1 implementation of watchlist CRUD."""

    def __init__(self, db: Any):
        self.db = db

    async def init_schema(self) -> None:
        for stmt in INIT_SCHEMA_STATEMENTS:
            await self.db.prepare(stmt).run()

    async def get_revision(self) -> int:
        row = await self.db.prepare(
            "SELECT value FROM config_meta WHERE key = 'revision'"
        ).first()
        if not row:
            return 0
        return int(row["value"])

    async def _assert_revision(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = await self.get_revision()
        if current != expected_revision:
            raise ConfigConflictError(
                f"watchlist changed from revision {expected_revision} to {current}"
            )

    async def _bump_revision(self) -> int:
        row = await self.db.prepare(
            "SELECT value FROM config_meta WHERE key = 'revision'"
        ).first()
        rev = int(row["value"]) + 1 if row else 1
        await self.db.prepare(
            "INSERT INTO config_meta (key, value, updated_at) VALUES ('revision', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        ).bind(str(rev), datetime.now(UTC).isoformat()).run()
        return rev

    async def _load_targets(self) -> list[TargetConfig]:
        result = await self.db.prepare(
            "SELECT * FROM targets ORDER BY sort_order ASC, name ASC"
        ).all()
        rows = result.results if hasattr(result, "results") else result
        return [target_row_to_model(dict(row)) for row in rows]

    async def _load_movies(self) -> list[MovieConfig]:
        result = await self.db.prepare(
            "SELECT * FROM movies ORDER BY sort_order ASC, key ASC"
        ).all()
        rows = result.results if hasattr(result, "results") else result
        return [movie_row_to_model(dict(row)) for row in rows]

    async def get_watchlist(self) -> dict[str, Any]:
        revision = await self.get_revision()
        targets = await self._load_targets()
        movies = await self._load_movies()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets],
            "movies": [m.model_dump(mode="json") for m in movies],
        }

    async def replace_watchlist(
        self,
        targets: list[TargetConfig],
        movies: list[MovieConfig],
        *,
        force: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)
        current_rev = await self.get_revision()
        if current_rev > 0 and not force and expected_revision is None:
            raise ConfigConflictError(
                "watchlist already seeded; pass --force or expected_revision to replace"
            )

        await self.db.prepare("DELETE FROM targets").run()
        await self.db.prepare("DELETE FROM movies").run()

        for idx, target in enumerate(targets):
            row = target_model_to_row(target, sort_order=idx)
            await self.db.prepare(
                "INSERT INTO targets (name, url, wait_until, timeout_ms, "
                "format_filter_click_selector, format_filter_click_label, "
                "format_filter_click_timeout_ms, direct_api_movie_id, direct_api_movie_title, "
                "direct_api_formats_json, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                row["name"],
                row["url"],
                row["wait_until"],
                row["timeout_ms"],
                row["format_filter_click_selector"],
                row["format_filter_click_label"],
                row["format_filter_click_timeout_ms"],
                row["direct_api_movie_id"],
                row["direct_api_movie_title"],
                row["direct_api_formats_json"],
                row["sort_order"],
            ).run()

        for idx, movie in enumerate(movies):
            row = movie_model_to_row(movie, sort_order=idx)
            await self.db.prepare(
                "INSERT INTO movies (key, title, fandango_movie_id, distributor, release_date, "
                "poster_url, fandango_targets_json, preferred_formats_json, x_handles_json, "
                "x_keywords_json, reference_page_key, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                row["key"],
                row["title"],
                row["fandango_movie_id"],
                row["distributor"],
                row["release_date"],
                row["poster_url"],
                row["fandango_targets_json"],
                row["preferred_formats_json"],
                row["x_handles_json"],
                row["x_keywords_json"],
                row["reference_page_key"],
                row["sort_order"],
            ).run()

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

    async def upsert_movie_with_targets(
        self,
        movie: MovieConfig,
        targets: list[TargetConfig],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)

        existing_movie = await self.db.prepare(
            "SELECT key FROM movies WHERE key = ?"
        ).bind(movie.key).first()
        if existing_movie:
            raise ValueError(f"movie key already exists: {movie.key!r}")

        for idx, target in enumerate(targets):
            row = target_model_to_row(target, sort_order=idx)
            await self.db.prepare(
                "INSERT INTO targets (name, url, wait_until, timeout_ms, "
                "format_filter_click_selector, format_filter_click_label, "
                "format_filter_click_timeout_ms, direct_api_movie_id, direct_api_movie_title, "
                "direct_api_formats_json, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "url = excluded.url, wait_until = excluded.wait_until, timeout_ms = excluded.timeout_ms, "
                "format_filter_click_selector = excluded.format_filter_click_selector, "
                "format_filter_click_label = excluded.format_filter_click_label, "
                "format_filter_click_timeout_ms = excluded.format_filter_click_timeout_ms, "
                "direct_api_movie_id = excluded.direct_api_movie_id, "
                "direct_api_movie_title = excluded.direct_api_movie_title, "
                "direct_api_formats_json = excluded.direct_api_formats_json, "
                "sort_order = excluded.sort_order"
            ).bind(
                row["name"],
                row["url"],
                row["wait_until"],
                row["timeout_ms"],
                row["format_filter_click_selector"],
                row["format_filter_click_label"],
                row["format_filter_click_timeout_ms"],
                row["direct_api_movie_id"],
                row["direct_api_movie_title"],
                row["direct_api_formats_json"],
                row["sort_order"],
            ).run()

        movie_count = await self.db.prepare("SELECT COUNT(*) AS n FROM movies").first()
        sort_order = int(movie_count["n"]) if movie_count else 0
        row = movie_model_to_row(movie, sort_order=sort_order)
        await self.db.prepare(
            "INSERT INTO movies (key, title, fandango_movie_id, distributor, release_date, "
            "poster_url, fandango_targets_json, preferred_formats_json, x_handles_json, "
            "x_keywords_json, reference_page_key, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            row["key"],
            row["title"],
            row["fandango_movie_id"],
            row["distributor"],
            row["release_date"],
            row["poster_url"],
            row["fandango_targets_json"],
            row["preferred_formats_json"],
            row["x_handles_json"],
            row["x_keywords_json"],
            row["reference_page_key"],
            row["sort_order"],
        ).run()

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

    async def patch_movie(
        self,
        key: str,
        patch: MoviePatch,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)
        row = await self.db.prepare("SELECT * FROM movies WHERE key = ?").bind(key).first()
        if not row:
            raise ValueError(f"movie not found: {key!r}")

        movie = movie_row_to_model(dict(row))
        updates = patch.model_dump(exclude_unset=True)
        merged = movie.model_copy(update=updates)
        out_row = movie_model_to_row(merged, sort_order=int(row.get("sort_order") or 0))
        await self.db.prepare(
            "UPDATE movies SET title = ?, fandango_movie_id = ?, distributor = ?, release_date = ?, "
            "poster_url = ?, fandango_targets_json = ?, preferred_formats_json = ?, "
            "x_handles_json = ?, x_keywords_json = ?, reference_page_key = ? "
            "WHERE key = ?"
        ).bind(
            out_row["title"],
            out_row["fandango_movie_id"],
            out_row["distributor"],
            out_row["release_date"],
            out_row["poster_url"],
            out_row["fandango_targets_json"],
            out_row["preferred_formats_json"],
            out_row["x_handles_json"],
            out_row["x_keywords_json"],
            out_row["reference_page_key"],
            key,
        ).run()
        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

    async def delete_movie(
        self,
        key: str,
        *,
        delete_owned_targets: bool = True,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)
        row = await self.db.prepare("SELECT * FROM movies WHERE key = ?").bind(key).first()
        if not row:
            raise ValueError(f"movie not found: {key!r}")

        movie = movie_row_to_model(dict(row))
        await self.db.prepare("DELETE FROM movies WHERE key = ?").bind(key).run()
        if delete_owned_targets:
            for target_name in movie.fandango_targets:
                await self.db.prepare("DELETE FROM targets WHERE name = ?").bind(target_name).run()

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

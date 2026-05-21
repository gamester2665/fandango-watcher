"""Stdlib-only D1 watchlist CRUD for the Cloudflare Worker bundle."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == 0:
        return None
    return int(value)


def _db_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _db_int_nullable(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    to_py = getattr(row, "to_py", None)
    if callable(to_py):
        data = to_py()
        if isinstance(data, dict):
            return data
    if isinstance(row, dict):
        return row
    return dict(row)


class ConfigConflictError(Exception):
    """Raised when an optimistic revision check fails."""


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

_PATCH_FIELDS = frozenset(
    {
        "title",
        "release_date",
        "poster_url",
        "preferred_formats",
        "x_handles",
        "x_keywords",
        "distributor",
        "reference_page_key",
    }
)


def _loads_json_list(raw: str | None, *, field: str) -> list[Any]:
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"{field} must be a JSON list, got {type(data).__name__}")
    return data


def _target_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(row["name"]),
        "url": str(row["url"]),
        "wait_until": row.get("wait_until") or "domcontentloaded",
        "timeout_ms": int(row.get("timeout_ms") or 30000),
        "format_filter_click_selector": _optional_text(row.get("format_filter_click_selector")),
        "format_filter_click_label": _optional_text(row.get("format_filter_click_label")),
        "format_filter_click_timeout_ms": int(row.get("format_filter_click_timeout_ms") or 12000),
        "direct_api_movie_id": _optional_int(row.get("direct_api_movie_id")),
        "direct_api_movie_title": _optional_text(row.get("direct_api_movie_title")),
        "direct_api_formats": _loads_json_list(
            row.get("direct_api_formats_json"), field="direct_api_formats_json"
        ),
    }


def _movie_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": str(row["key"]),
        "title": str(row["title"]),
        "fandango_movie_id": _optional_int(row.get("fandango_movie_id")),
        "distributor": _optional_text(row.get("distributor")),
        "release_date": _optional_text(row.get("release_date")),
        "poster_url": _optional_text(row.get("poster_url")),
        "fandango_targets": [
            str(v)
            for v in _loads_json_list(row.get("fandango_targets_json"), field="fandango_targets_json")
        ],
        "preferred_formats": [
            str(v)
            for v in _loads_json_list(row.get("preferred_formats_json"), field="preferred_formats_json")
        ],
        "x_handles": [
            str(v) for v in _loads_json_list(row.get("x_handles_json"), field="x_handles_json")
        ],
        "x_keywords": [
            str(v) for v in _loads_json_list(row.get("x_keywords_json"), field="x_keywords_json")
        ],
        "reference_page_key": _optional_text(row.get("reference_page_key")),
    }


def _target_to_db(target: dict[str, Any], *, sort_order: int) -> dict[str, Any]:
    return {
        "name": target["name"],
        "url": target["url"],
        "wait_until": target.get("wait_until") or "domcontentloaded",
        "timeout_ms": int(target.get("timeout_ms") or 30000),
        "format_filter_click_selector": _db_text(target.get("format_filter_click_selector")),
        "format_filter_click_label": _db_text(target.get("format_filter_click_label")),
        "format_filter_click_timeout_ms": int(target.get("format_filter_click_timeout_ms") or 12000),
        "direct_api_movie_id": _db_int_nullable(target.get("direct_api_movie_id")),
        "direct_api_movie_title": _db_text(target.get("direct_api_movie_title")),
        "direct_api_formats_json": json.dumps(list(target.get("direct_api_formats") or [])),
        "sort_order": sort_order,
    }


def _movie_to_db(movie: dict[str, Any], *, sort_order: int) -> dict[str, Any]:
    return {
        "key": movie["key"],
        "title": movie["title"],
        "fandango_movie_id": _db_int_nullable(movie.get("fandango_movie_id")),
        "distributor": _db_text(movie.get("distributor")),
        "release_date": _db_text(movie.get("release_date")),
        "poster_url": _db_text(movie.get("poster_url")),
        "fandango_targets_json": json.dumps(list(movie.get("fandango_targets") or [])),
        "preferred_formats_json": json.dumps(list(movie.get("preferred_formats") or [])),
        "x_handles_json": json.dumps(list(movie.get("x_handles") or [])),
        "x_keywords_json": json.dumps(list(movie.get("x_keywords") or [])),
        "reference_page_key": _db_text(movie.get("reference_page_key")),
        "sort_order": sort_order,
    }


def _normalize_target(raw: dict[str, Any]) -> dict[str, Any]:
    name = raw.get("name")
    url = raw.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("target.name is required")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("target.url is required")
    return _target_to_db({"name": name.strip(), "url": url.strip(), **raw}, sort_order=0)


def _normalize_movie(raw: dict[str, Any]) -> dict[str, Any]:
    key = raw.get("key")
    title = raw.get("title")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("movie.key is required")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("movie.title is required")
    targets = raw.get("fandango_targets") or []
    if not isinstance(targets, list):
        raise ValueError("movie.fandango_targets must be a list")
    return _movie_to_db(
        {
            "key": key.strip(),
            "title": title.strip(),
            "fandango_targets": [str(v) for v in targets],
            "preferred_formats": [str(v) for v in (raw.get("preferred_formats") or [])],
            "x_handles": [str(v) for v in (raw.get("x_handles") or [])],
            "x_keywords": [str(v) for v in (raw.get("x_keywords") or [])],
            **raw,
        },
        sort_order=0,
    )


def _normalize_patch(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - _PATCH_FIELDS - {"expected_revision"}
    if unknown:
        raise ValueError(f"unsupported patch fields: {sorted(unknown)}")
    return {k: v for k, v in raw.items() if k in _PATCH_FIELDS}


class D1WatchlistProvider:
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
        return int(_as_dict(row)["value"])

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
        rev = int(_as_dict(row)["value"]) + 1 if row else 1
        await self.db.prepare(
            "INSERT INTO config_meta (key, value, updated_at) VALUES ('revision', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        ).bind(str(rev), datetime.now(UTC).isoformat()).run()
        return rev

    async def _load_targets(self) -> list[dict[str, Any]]:
        result = await self.db.prepare(
            "SELECT * FROM targets ORDER BY sort_order ASC, name ASC"
        ).all()
        rows = result.results if hasattr(result, "results") else result
        return [_target_row(_as_dict(row)) for row in rows]

    async def _load_movies(self) -> list[dict[str, Any]]:
        result = await self.db.prepare(
            "SELECT * FROM movies ORDER BY sort_order ASC, key ASC"
        ).all()
        rows = result.results if hasattr(result, "results") else result
        return [_movie_row(_as_dict(row)) for row in rows]

    async def get_watchlist(self) -> dict[str, Any]:
        return {
            "revision": await self.get_revision(),
            "targets": await self._load_targets(),
            "movies": await self._load_movies(),
        }

    async def _insert_target(self, row: dict[str, Any]) -> None:
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

    async def _insert_movie(self, row: dict[str, Any]) -> None:
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

    async def replace_watchlist(
        self,
        targets: list[dict[str, Any]],
        movies: list[dict[str, Any]],
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
            row = _normalize_target(target)
            row["sort_order"] = idx
            await self._insert_target(row)

        for idx, movie in enumerate(movies):
            row = _normalize_movie(movie)
            row["sort_order"] = idx
            await self._insert_movie(row)

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

    async def upsert_movie_with_targets(
        self,
        movie: dict[str, Any],
        targets: list[dict[str, Any]],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)
        movie_row = _normalize_movie(movie)

        existing_movie = await self.db.prepare(
            "SELECT key FROM movies WHERE key = ?"
        ).bind(movie_row["key"]).first()
        if existing_movie:
            raise ValueError(f"movie key already exists: {movie_row['key']!r}")

        for idx, target in enumerate(targets):
            row = _normalize_target(target)
            row["sort_order"] = idx
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
        movie_row["sort_order"] = int(_as_dict(movie_count).get("n") or 0)
        await self._insert_movie(movie_row)

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

    async def patch_movie(
        self,
        key: str,
        patch: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        await self._assert_revision(expected_revision)
        row = _as_dict(await self.db.prepare("SELECT * FROM movies WHERE key = ?").bind(key).first())
        if not row:
            raise ValueError(f"movie not found: {key!r}")

        movie = _movie_row(row)
        updates = _normalize_patch(patch)
        merged = {**movie, **updates}
        out_row = _movie_to_db(merged, sort_order=int(row.get("sort_order") or 0))
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
        row = _as_dict(await self.db.prepare("SELECT * FROM movies WHERE key = ?").bind(key).first())
        if not row:
            raise ValueError(f"movie not found: {key!r}")

        movie = _movie_row(row)
        await self.db.prepare("DELETE FROM movies WHERE key = ?").bind(key).run()
        if delete_owned_targets:
            for target_name in movie["fandango_targets"]:
                await self.db.prepare("DELETE FROM targets WHERE name = ?").bind(target_name).run()

        revision = await self._bump_revision()
        return {"revision": revision, **(await self.get_watchlist())}

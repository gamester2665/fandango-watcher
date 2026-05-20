"""Local SQLite watchlist store (same schema as Cloudflare D1)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cloudflare_config import (
    INIT_SCHEMA_STATEMENTS,
    ConfigConflictError,
    MoviePatch,
    movie_model_to_row,
    movie_row_to_model,
    target_model_to_row,
    target_row_to_model,
)
from .config import MovieConfig, TargetConfig

_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    return _LOCKS.setdefault(key, threading.RLock())


class SqliteWatchlistProvider:
    """SQLite implementation of watchlist CRUD for local/VPS config API."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                for stmt in INIT_SCHEMA_STATEMENTS:
                    conn.execute(stmt)
                conn.commit()
            finally:
                conn.close()

    def get_revision(self) -> int:
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM config_meta WHERE key = 'revision'"
                ).fetchone()
                return int(row["value"]) if row else 0
            finally:
                conn.close()

    def _assert_revision(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = self.get_revision()
        if current != expected_revision:
            raise ConfigConflictError(
                f"watchlist changed from revision {expected_revision} to {current}"
            )

    def _bump_revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM config_meta WHERE key = 'revision'"
        ).fetchone()
        rev = int(row["value"]) + 1 if row else 1
        conn.execute(
            "INSERT INTO config_meta (key, value, updated_at) VALUES ('revision', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (str(rev), datetime.now(UTC).isoformat()),
        )
        return rev

    def _load_targets(self, conn: sqlite3.Connection) -> list[TargetConfig]:
        rows = conn.execute(
            "SELECT * FROM targets ORDER BY sort_order ASC, name ASC"
        ).fetchall()
        return [target_row_to_model(dict(row)) for row in rows]

    def _load_movies(self, conn: sqlite3.Connection) -> list[MovieConfig]:
        rows = conn.execute(
            "SELECT * FROM movies ORDER BY sort_order ASC, key ASC"
        ).fetchall()
        return [movie_row_to_model(dict(row)) for row in rows]

    def get_watchlist(self) -> dict[str, Any]:
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                revision = self.get_revision()
                targets = self._load_targets(conn)
                movies = self._load_movies(conn)
            finally:
                conn.close()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets],
            "movies": [m.model_dump(mode="json") for m in movies],
        }

    def replace_watchlist(
        self,
        targets: list[TargetConfig],
        movies: list[MovieConfig],
        *,
        force: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_revision(expected_revision)
        current_rev = self.get_revision()
        if current_rev > 0 and not force and expected_revision is None:
            raise ConfigConflictError(
                "watchlist already seeded; pass --force or expected_revision to replace"
            )

        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                conn.execute("DELETE FROM targets")
                conn.execute("DELETE FROM movies")
                for idx, target in enumerate(targets):
                    row = target_model_to_row(target, sort_order=idx)
                    conn.execute(
                        "INSERT INTO targets (name, url, wait_until, timeout_ms, "
                        "format_filter_click_selector, format_filter_click_label, "
                        "format_filter_click_timeout_ms, direct_api_movie_id, direct_api_movie_title, "
                        "direct_api_formats_json, sort_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
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
                        ),
                    )
                for idx, movie in enumerate(movies):
                    row = movie_model_to_row(movie, sort_order=idx)
                    conn.execute(
                        "INSERT INTO movies (key, title, fandango_movie_id, distributor, release_date, "
                        "poster_url, fandango_targets_json, preferred_formats_json, x_handles_json, "
                        "x_keywords_json, reference_page_key, sort_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
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
                        ),
                    )
                revision = self._bump_revision(conn)
                conn.commit()
                targets_out = self._load_targets(conn)
                movies_out = self._load_movies(conn)
            finally:
                conn.close()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets_out],
            "movies": [m.model_dump(mode="json") for m in movies_out],
        }

    def upsert_movie_with_targets(
        self,
        movie: MovieConfig,
        targets: list[TargetConfig],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_revision(expected_revision)
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT key FROM movies WHERE key = ?", (movie.key,)
                ).fetchone()
                if existing:
                    raise ValueError(f"movie key already exists: {movie.key!r}")

                for idx, target in enumerate(targets):
                    row = target_model_to_row(target, sort_order=idx)
                    conn.execute(
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
                        "sort_order = excluded.sort_order",
                        (
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
                        ),
                    )

                movie_count = conn.execute("SELECT COUNT(*) AS n FROM movies").fetchone()
                sort_order = int(movie_count["n"]) if movie_count else 0
                row = movie_model_to_row(movie, sort_order=sort_order)
                conn.execute(
                    "INSERT INTO movies (key, title, fandango_movie_id, distributor, release_date, "
                    "poster_url, fandango_targets_json, preferred_formats_json, x_handles_json, "
                    "x_keywords_json, reference_page_key, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
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
                    ),
                )
                revision = self._bump_revision(conn)
                conn.commit()
                targets_out = self._load_targets(conn)
                movies_out = self._load_movies(conn)
            finally:
                conn.close()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets_out],
            "movies": [m.model_dump(mode="json") for m in movies_out],
        }

    def patch_movie(
        self,
        key: str,
        patch: MoviePatch,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_revision(expected_revision)
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM movies WHERE key = ?", (key,)).fetchone()
                if not row:
                    raise ValueError(f"movie not found: {key!r}")
                movie = movie_row_to_model(dict(row))
                merged = movie.model_copy(update=patch.model_dump(exclude_unset=True))
                out_row = movie_model_to_row(merged, sort_order=int(row["sort_order"] or 0))
                conn.execute(
                    "UPDATE movies SET title = ?, fandango_movie_id = ?, distributor = ?, release_date = ?, "
                    "poster_url = ?, fandango_targets_json = ?, preferred_formats_json = ?, "
                    "x_handles_json = ?, x_keywords_json = ?, reference_page_key = ? "
                    "WHERE key = ?",
                    (
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
                    ),
                )
                revision = self._bump_revision(conn)
                conn.commit()
                targets_out = self._load_targets(conn)
                movies_out = self._load_movies(conn)
            finally:
                conn.close()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets_out],
            "movies": [m.model_dump(mode="json") for m in movies_out],
        }

    def delete_movie(
        self,
        key: str,
        *,
        delete_owned_targets: bool = True,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._assert_revision(expected_revision)
        with _lock_for(self.db_path):
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM movies WHERE key = ?", (key,)).fetchone()
                if not row:
                    raise ValueError(f"movie not found: {key!r}")
                movie = movie_row_to_model(dict(row))
                conn.execute("DELETE FROM movies WHERE key = ?", (key,))
                if delete_owned_targets:
                    for target_name in movie.fandango_targets:
                        conn.execute("DELETE FROM targets WHERE name = ?", (target_name,))
                revision = self._bump_revision(conn)
                conn.commit()
                targets_out = self._load_targets(conn)
                movies_out = self._load_movies(conn)
            finally:
                conn.close()
        return {
            "revision": revision,
            "targets": [t.model_dump(mode="json") for t in targets_out],
            "movies": [m.model_dump(mode="json") for m in movies_out],
        }

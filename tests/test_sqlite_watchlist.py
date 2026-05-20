"""Tests for local SQLite watchlist CRUD."""

from __future__ import annotations

from pathlib import Path

from fandango_watcher.config import MovieConfig, TargetConfig
from fandango_watcher.models import FormatTag
from fandango_watcher.sqlite_watchlist import SqliteWatchlistProvider


def test_sqlite_replace_and_revision(tmp_path: Path) -> None:
    db = tmp_path / "watchlist.db"
    provider = SqliteWatchlistProvider(db)
    provider.init_schema()

    targets = [
        TargetConfig(
            name="odyssey-overview",
            url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
        )
    ]
    movies = [
        MovieConfig(
            key="odyssey",
            title="The Odyssey (2026)",
            fandango_targets=["odyssey-overview"],
            preferred_formats=[FormatTag.IMAX_70MM],
        )
    ]

    result = provider.replace_watchlist(targets, movies, force=True)
    assert result["revision"] == 1
    assert len(result["movies"]) == 1
    assert provider.get_revision() == 1

    watchlist = provider.get_watchlist()
    assert watchlist["revision"] == 1
    assert watchlist["movies"][0]["key"] == "odyssey"

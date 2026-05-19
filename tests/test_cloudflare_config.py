"""Tests for D1 watchlist row mapping."""

from __future__ import annotations

import pytest

from fandango_watcher.cloudflare_config import (
    ConfigConflictError,
    movie_model_to_row,
    movie_row_to_model,
    target_model_to_row,
    target_row_to_model,
)
from fandango_watcher.config import MovieConfig, TargetConfig
from fandango_watcher.models import FormatTag


def test_target_row_round_trip() -> None:
    target = TargetConfig(
        name="odyssey-overview",
        url="https://www.fandango.com/the-odyssey-2026-241283/movie-overview",
        direct_api_formats=[FormatTag.IMAX_70MM],
    )
    row = target_model_to_row(target, sort_order=3)
    restored = target_row_to_model(row)
    assert restored == target


def test_movie_row_round_trip() -> None:
    movie = MovieConfig(
        key="odyssey",
        title="The Odyssey (2026)",
        fandango_movie_id=241283,
        fandango_targets=["odyssey-overview", "odyssey-imax-70mm"],
        preferred_formats=[FormatTag.IMAX_70MM, FormatTag.IMAX],
        x_handles=["TheOdysseyFilm"],
        x_keywords=["tickets", "odyssey"],
    )
    row = movie_model_to_row(movie, sort_order=1)
    restored = movie_row_to_model(row)
    assert restored == movie


def test_movie_row_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        movie_row_to_model(
            {
                "key": "bad",
                "title": "Bad",
                "fandango_targets_json": "{not json",
                "preferred_formats_json": "[]",
                "x_handles_json": "[]",
                "x_keywords_json": "[]",
            }
        )


def test_config_conflict_error_message() -> None:
    err = ConfigConflictError("watchlist changed from revision 1 to 2")
    assert "revision 1" in str(err)

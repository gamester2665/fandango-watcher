"""Tests for watchlist config API client routing (Cloudflare Worker vs SQLite)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fandango_watcher.config import MovieConfig, RemoteWatchlist, Settings, TargetConfig
from fandango_watcher.config_api_client import (
    ConfigApiError,
    config_writes_enabled,
    fetch_watchlist_for_settings,
    remote_add_movie,
    remote_delete_movie,
    remote_patch_movie,
    remote_replace_watchlist,
    watchlist_config_source,
)


def _settings(
    *,
    config_api_url: str = "",
    config_local_db_path: str = "",
    config_admin_token: str = "secret",
) -> Settings:
    return Settings(
        config_api_url=config_api_url,
        config_local_db_path=config_local_db_path,
        config_admin_token=config_admin_token,
    )


def _remote_watchlist() -> RemoteWatchlist:
    return RemoteWatchlist(
        revision=5,
        targets=[TargetConfig(name="worker-target", url="https://example.com/worker")],
        movies=[
            MovieConfig(
                key="worker-movie",
                title="Worker Movie",
                fandango_targets=["worker-target"],
            )
        ],
    )


def test_watchlist_config_source_prefers_worker_url() -> None:
    settings = _settings(
        config_api_url="https://fandango-watcher.example.workers.dev",
        config_local_db_path="/app/state/watchlist.db",
    )
    assert watchlist_config_source(settings) == "d1"


def test_watchlist_config_source_sqlite_without_worker_url() -> None:
    settings = _settings(config_local_db_path="/app/state/watchlist.db")
    assert watchlist_config_source(settings) == "sqlite"


def test_config_writes_enabled_with_worker_url_only() -> None:
    settings = _settings(
        config_api_url="https://fandango-watcher.example.workers.dev",
        config_admin_token="token",
    )
    assert config_writes_enabled(settings) is True


def test_config_writes_disabled_without_admin_token() -> None:
    settings = _settings(
        config_api_url="https://fandango-watcher.example.workers.dev",
        config_admin_token="",
    )
    assert config_writes_enabled(settings) is False


def test_fetch_watchlist_prefers_worker_url_over_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        config_api_url="https://worker.example",
        config_local_db_path="/tmp/watchlist.db",
    )
    remote = _remote_watchlist()
    http_calls: list[str] = []
    local_called = False

    def _fake_http(url: str, *, timeout: float = 15.0) -> RemoteWatchlist:
        http_calls.append(url)
        return remote

    def _fake_local(_db_path: str) -> RemoteWatchlist:
        nonlocal local_called
        local_called = True
        raise AssertionError("sqlite fetch should not run when worker URL is set")

    monkeypatch.setattr(
        "fandango_watcher.config_api_client.fetch_watchlist_http",
        _fake_http,
    )
    monkeypatch.setattr(
        "fandango_watcher.config_api_client.fetch_watchlist_local",
        _fake_local,
    )

    loaded = fetch_watchlist_for_settings(settings)
    assert loaded.revision == 5
    assert loaded.movies[0].key == "worker-movie"
    assert http_calls == ["https://worker.example"]
    assert local_called is False


def test_fetch_watchlist_uses_sqlite_when_worker_url_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "watchlist.db"
    settings = _settings(config_local_db_path=str(db_path))
    remote = _remote_watchlist()
    http_called = False

    def _fake_http(_url: str, *, timeout: float = 15.0) -> RemoteWatchlist:
        nonlocal http_called
        http_called = True
        raise AssertionError("HTTP fetch should not run without worker URL")

    monkeypatch.setattr(
        "fandango_watcher.config_api_client.fetch_watchlist_http",
        _fake_http,
    )
    monkeypatch.setattr(
        "fandango_watcher.config_api_client.fetch_watchlist_local",
        lambda path: remote if path == str(db_path) else (_ for _ in ()).throw(
            AssertionError("unexpected db path")
        ),
    )

    loaded = fetch_watchlist_for_settings(settings)
    assert loaded.revision == 5
    assert http_called is False


@pytest.mark.parametrize(
    ("remote_fn", "args", "method", "path"),
    [
        (
            remote_add_movie,
            ({"title": "New Movie", "url": "https://www.fandango.com/x/movie-overview"},),
            "POST",
            "/api/movies",
        ),
        (
            remote_patch_movie,
            ("movie-key", {"title": "Updated"}),
            "PATCH",
            "/api/movies/movie-key",
        ),
        (
            remote_delete_movie,
            ("movie-key",),
            "DELETE",
            "/api/movies/movie-key",
        ),
    ],
)
def test_remote_crud_prefers_worker_url_over_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    remote_fn,
    args: tuple,
    method: str,
    path: str,
) -> None:
    settings = _settings(
        config_api_url="https://worker.example",
        config_local_db_path="/tmp/watchlist.db",
    )
    calls: list[tuple[str, str]] = []
    local_called = False

    def _fake_admin_json_request(req_method: str, req_path: str, _payload, _settings):
        calls.append((req_method, req_path))
        return {"ok": True, "revision": 9}

    def _mark_local(*_a, **_kw):
        nonlocal local_called
        local_called = True
        raise AssertionError("sqlite CRUD should not run when worker URL is set")

    monkeypatch.setattr(
        "fandango_watcher.config_api_client.admin_json_request",
        _fake_admin_json_request,
    )
    monkeypatch.setattr(
        "fandango_watcher.config_api_client._local_add_movie",
        _mark_local,
    )
    monkeypatch.setattr(
        "fandango_watcher.config_api_client._local_patch_movie",
        _mark_local,
    )
    monkeypatch.setattr(
        "fandango_watcher.config_api_client._local_delete_movie",
        _mark_local,
    )

    if remote_fn is remote_delete_movie:
        result = remote_fn(settings, *args, expected_revision=4)
    else:
        result = remote_fn(settings, *args)

    assert result["revision"] == 9
    assert calls == [(method, path)]
    assert local_called is False


def test_remote_replace_watchlist_prefers_worker_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        config_api_url="https://worker.example",
        config_local_db_path="/tmp/watchlist.db",
    )
    calls: list[tuple[str, str]] = []

    def _fake_admin_json_request(req_method: str, req_path: str, _payload, _settings):
        calls.append((req_method, req_path))
        return {"ok": True, "revision": 2}

    monkeypatch.setattr(
        "fandango_watcher.config_api_client.admin_json_request",
        _fake_admin_json_request,
    )
    monkeypatch.setattr(
        "fandango_watcher.sqlite_watchlist.SqliteWatchlistProvider",
        MagicMock(side_effect=AssertionError("sqlite replace should not run")),
    )

    targets = [TargetConfig(name="t1", url="https://example.com/t1")]
    movies = [MovieConfig(key="m1", title="M1", fandango_targets=["t1"])]
    result = remote_replace_watchlist(settings, targets, movies, force=True)
    assert result["revision"] == 2
    assert calls == [("POST", "/api/watchlist/replace")]


def test_remote_add_movie_requires_backend() -> None:
    settings = _settings()
    with pytest.raises(ConfigApiError, match="CONFIG_API_URL or CONFIG_LOCAL_DB_PATH"):
        remote_add_movie(settings, {"title": "X", "url": "https://example.com/x"})

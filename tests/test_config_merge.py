"""Tests for YAML + remote watchlist merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from fandango_watcher.config import (
    MovieConfig,
    RemoteWatchlist,
    TargetConfig,
    WatcherConfig,
    load_config,
    merge_watchlist,
)
from fandango_watcher.config_api_client import (
    load_config_merged,
    read_watchlist_cache,
    write_watchlist_cache,
)
from fandango_watcher.config import Settings


def _minimal_policy_dict() -> dict:
    return {
        "targets": [
            {
                "name": "placeholder",
                "url": "https://example.com/placeholder",
            }
        ],
        "theater": {
            "display_name": "CW",
            "fandango_theater_anchor": "AMC Universal CityWalk",
        },
        "formats": {"require": [], "include": []},
        "poll": {"min_seconds": 30, "max_seconds": 35},
        "notify": {"channels": [], "on_events": []},
        "screenshots": {"dir": "artifacts/screenshots"},
        "state": {"dir": "state"},
        "purchase": {"enabled": True, "mode": "notify_only"},
        "movies": [],
    }


def test_merge_watchlist_replaces_targets_and_movies(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
targets:
  - name: yaml-only
    url: https://example.com/yaml
theater:
  display_name: CW
  fandango_theater_anchor: AMC Universal CityWalk
formats:
  require: []
  include: []
poll:
  min_seconds: 30
  max_seconds: 35
notify:
  channels: []
  on_events: []
screenshots:
  dir: artifacts/screenshots
state:
  dir: state
purchase:
  enabled: true
  mode: notify_only
movies: []
""".lstrip(),
        encoding="utf-8",
    )
    base = load_config(config_path)
    remote_targets = [
        TargetConfig(name="remote-overview", url="https://example.com/remote"),
    ]
    remote_movies = [
        MovieConfig(
            key="remote",
            title="Remote Movie",
            fandango_targets=["remote-overview"],
        )
    ]
    merged = merge_watchlist(base, remote_targets, remote_movies)
    assert [t.name for t in merged.targets] == ["remote-overview"]
    assert merged.movies[0].key == "remote"
    assert merged.theater.display_name == "CW"


def test_merge_watchlist_rejects_orphan_target_reference() -> None:
    base = WatcherConfig.model_validate(_minimal_policy_dict())
    with pytest.raises(ValueError, match="references unknown target"):
        merge_watchlist(
            base,
            [TargetConfig(name="existing", url="https://example.com/existing")],
            [
                MovieConfig(
                    key="bad",
                    title="Bad",
                    fandango_targets=["missing-target"],
                )
            ],
        )


def test_load_config_merged_uses_cache_when_api_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
targets:
  - name: yaml-only
    url: https://example.com/yaml
theater:
  display_name: CW
  fandango_theater_anchor: AMC Universal CityWalk
formats:
  require: []
  include: []
poll:
  min_seconds: 30
  max_seconds: 35
notify:
  channels: []
  on_events: []
screenshots:
  dir: artifacts/screenshots
state:
  dir: state
purchase:
  enabled: true
  mode: notify_only
movies: []
""".lstrip(),
        encoding="utf-8",
    )
    cache_path = tmp_path / "watchlist-cache.json"
    remote = RemoteWatchlist(
        revision=7,
        targets=[TargetConfig(name="cached", url="https://example.com/cached")],
        movies=[
            MovieConfig(
                key="cached",
                title="Cached Movie",
                fandango_targets=["cached"],
            )
        ],
    )
    write_watchlist_cache(cache_path, remote, source="https://worker.example")

    def _fail_fetch(_url: str, *, timeout: float = 15.0) -> RemoteWatchlist:
        raise RuntimeError("network down")

    monkeypatch.setenv("CONFIG_API_URL", "https://worker.example")
    monkeypatch.setenv("CONFIG_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(
        "fandango_watcher.config_api_client.fetch_watchlist_http",
        _fail_fetch,
    )

    settings = Settings()
    cfg, revision, meta = load_config_merged(config_path, settings)
    assert revision == 7
    assert meta["config_source"] == "d1-cache"
    assert cfg.targets[0].name == "cached"


def test_read_watchlist_cache_round_trip(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    remote = RemoteWatchlist(
        revision=2,
        targets=[TargetConfig(name="a", url="https://example.com/a")],
        movies=[MovieConfig(key="a", title="A", fandango_targets=["a"])],
    )
    write_watchlist_cache(cache_path, remote, source="https://worker.example")
    loaded, meta = read_watchlist_cache(cache_path)
    assert loaded.revision == 2
    assert loaded.targets[0].name == "a"
    assert meta["source"] == "https://worker.example"

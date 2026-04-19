"""Tests for src/fandango_watcher/release_intel.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fandango_watcher.config import (
    BrowserConfig,
    MovieConfig,
    NotifyConfig,
    PollConfig,
    PurchaseConfig,
    ReleaseIntelConfig,
    ScreenshotsConfig,
    Settings,
    StateConfig,
    TargetConfig,
    TheaterConfig,
    ViewportConfig,
    WatcherConfig,
)
from fandango_watcher.release_intel import (
    _compose_prompt,
    _extract_json_object,
    get_release_intel_for_dashboard,
    refresh_release_intel,
)


def _cfg(tmp: Path) -> WatcherConfig:
    st = tmp / "state"
    st.mkdir(parents=True)
    (st / "mandalorian-overview.json").write_text(
        json.dumps(
            {
                "target_name": "mandalorian-overview",
                "last_release_schema": "full_release",
                "current_state": "alerted",
            }
        ),
        encoding="utf-8",
    )
    return WatcherConfig(
        targets=[
            TargetConfig(
                name="mandalorian-overview",
                url="https://www.fandango.com/x",
            ),
        ],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="AMC"),
        formats={"require": [], "include": []},  # type: ignore[arg-type]
        poll=PollConfig(min_seconds=30, max_seconds=35),
        purchase=PurchaseConfig(),
        notify=NotifyConfig(channels=[], on_events=[]),
        screenshots=ScreenshotsConfig(
            dir=str(tmp / "art" / "screenshots"),
            per_purchase_dir=str(tmp / "art" / "buy"),
        ),
        state=StateConfig(dir=str(st)),
        browser=BrowserConfig(
            user_data_dir=str(tmp / "prof"),
            record_video_dir=str(tmp / "art" / "vid"),
            record_trace_dir=str(tmp / "art" / "tr"),
            viewport=ViewportConfig(),
        ),
        movies=[
            MovieConfig(
                key="mandalorian_and_grogu",
                title="The Mandalorian and Grogu",
                fandango_targets=["mandalorian-overview"],
                x_handles=["starwars"],
                x_keywords=["tickets"],
            ),
        ],
        release_intel=ReleaseIntelConfig(
            cache_ttl_seconds=3600,
            timeout_seconds=30,
        ),
    )


def test_extract_json_object_strips_fence() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _extract_json_object(raw) == {"a": 1}


def test_get_release_intel_unconfigured_without_key(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    got = get_release_intel_for_dashboard(
        cfg,
        state_dir=Path(cfg.state.dir),
        settings=Settings(xai_api_key=""),
    )
    assert got["status"] == "unconfigured"


def test_get_release_intel_disabled_in_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "release_intel": ReleaseIntelConfig(enabled=False),
        }
    )
    got = get_release_intel_for_dashboard(
        cfg,
        state_dir=Path(cfg.state.dir),
        settings=Settings(xai_api_key="sk-test"),
    )
    assert got["status"] == "disabled"


def test_refresh_release_intel_writes_cache(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    state_dir = Path(cfg.state.dir)
    fake_llm = json.dumps(
        {
            "movies": {
                "mandalorian_and_grogu": {
                    "headline": "Tickets live",
                    "summary": "Wide release window.",
                    "ticketing": "On sale",
                    "notable_dates": "May 2026",
                    "qualifier": "Advisory",
                }
            }
        }
    )

    with patch(
        "fandango_watcher.release_intel._call_xai",
        return_value=fake_llm,
    ):
        out = refresh_release_intel(
            cfg,
            state_dir=state_dir,
            settings=Settings(xai_api_key="sk-fake"),
        )

    assert "mandalorian_and_grogu" in out["movies"]
    cache_file = state_dir / "release_intel_cache.json"
    assert cache_file.is_file()
    disk = json.loads(cache_file.read_text(encoding="utf-8"))
    assert disk["movies"]["mandalorian_and_grogu"]["headline"] == "Tickets live"


def test_compose_prompt_contains_movie_key() -> None:
    rows = [{"key": "mandalorian_and_grogu", "title": "Mando", "fandango_targets": []}]
    p = _compose_prompt(rows)
    assert "mandalorian_and_grogu" in p
    assert "JSON" in p

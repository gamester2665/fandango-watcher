"""Tests for src/fandango_watcher/dashboard.py."""

from __future__ import annotations

import json
from pathlib import Path

from fandango_watcher.config import (
    BrowserConfig,
    NotifyConfig,
    PollConfig,
    PurchaseConfig,
    ScreenshotsConfig,
    StateConfig,
    TargetConfig,
    TheaterConfig,
    ViewportConfig,
    WatcherConfig,
)
from fandango_watcher.dashboard import (
    DashboardData,
    DashboardPaths,
    _latest_artifact_for_target,
    artifact_url,
    collect_dashboard_state,
    render_index_html,
)
from fandango_watcher.healthz import Heartbeat


def _cfg(tmp_path: Path) -> WatcherConfig:
    return WatcherConfig(
        targets=[
            TargetConfig(name="alpha", url="https://example.com/a"),
        ],
        theater=TheaterConfig(
            display_name="CW",
            fandango_theater_anchor="AMC Universal CityWalk",
        ),
        formats={"require": [], "include": []},  # type: ignore[arg-type]
        poll=PollConfig(min_seconds=30, max_seconds=35),
        purchase=PurchaseConfig(),
        notify=NotifyConfig(channels=[], on_events=[]),
        screenshots=ScreenshotsConfig(
            dir=str(tmp_path / "artifacts" / "screenshots"),
            per_purchase_dir=str(tmp_path / "artifacts" / "purchase-attempts"),
        ),
        state=StateConfig(dir=str(tmp_path / "state")),
        browser=BrowserConfig(
            user_data_dir=str(tmp_path / "profile"),
            record_video_dir=str(tmp_path / "artifacts" / "videos"),
            record_trace_dir=str(tmp_path / "artifacts" / "traces"),
            viewport=ViewportConfig(),
        ),
    )


def test_latest_artifact_newest_by_mtime(tmp_path: Path) -> None:
    d = tmp_path / "shots"
    d.mkdir(parents=True)
    (d / "alpha-old.png").write_bytes(b"x")
    (d / "alpha-new.png").write_bytes(b"xx")
    (d / "beta-1.png").write_bytes(b"y")

    got = _latest_artifact_for_target("alpha", d, ".png")
    assert got is not None
    assert got.name == "alpha-new.png"


def test_collect_dashboard_state_roundtrip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    paths = DashboardPaths.from_config(cfg)
    state_dir = paths.state_dir
    state_dir.mkdir(parents=True)
    (state_dir / "alpha.json").write_text(
        json.dumps(
            {
                "target_name": "alpha",
                "current_state": "watching",
                "last_release_schema": "not_on_sale",
                "total_ticks": 3,
            }
        ),
        encoding="utf-8",
    )
    paths.screenshot_dir.mkdir(parents=True)
    (paths.screenshot_dir / "alpha-20260101T000000Z.png").write_bytes(b"png")

    hb = Heartbeat()
    snap = collect_dashboard_state(
        DashboardData(cfg=cfg, paths=paths, heartbeat=hb)
    )
    assert snap["healthz"]["total_ticks"] == 0
    assert len(snap["targets"]) == 1
    t0 = snap["targets"][0]
    assert t0["name"] == "alpha"
    assert t0["state"]["total_ticks"] == 3
    assert t0["latest_screenshot_url"] is not None
    assert t0["latest_screenshot_url"].startswith("/artifacts/")


def test_render_index_html_escapes_movie_title() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "movies": [{"key": "m1", "title": "<script>evil</script>", "fandango_targets": [], "x_handles": []}],
    }
    html_out = render_index_html(snap)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out or "evil" in html_out


def test_artifact_url_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    sub = root / "screenshots"
    sub.mkdir()
    f = sub / "x.png"
    f.write_bytes(b"1")
    assert artifact_url(root, f) == "/artifacts/screenshots/x.png"
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    assert artifact_url(root, outside) is None

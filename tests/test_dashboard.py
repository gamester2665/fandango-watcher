"""Tests for src/fandango_watcher/dashboard.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    _relative_ago,
    artifact_url,
    collect_dashboard_state,
    compute_dashboard_revision,
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
    assert "release_intel" in snap
    assert snap["release_intel"]["status"] == "unconfigured"
    assert snap.get("purchases_history") == []
    assert "purchases_jsonl" in (snap.get("paths") or {})
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
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [{"key": "m1", "title": "<script>evil</script>", "fandango_targets": [], "x_handles": []}],
    }
    html_out = render_index_html(snap)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out or "evil" in html_out


def test_render_index_html_shows_empty_state_hint() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [{"name": "t1", "url": "https://x", "state": {}}],
        "social_x": {"handles": {}},
        "release_intel": {"status": "disabled", "reason": "test"},
        "movies": [],
    }
    html_out = render_index_html(snap)
    assert "No per-target crawl history yet" in html_out
    assert "fandango-watcher watch" in html_out


def test_relative_ago_minutes() -> None:
    now = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)
    past = datetime(2026, 6, 1, 14, 20, 0, tzinfo=UTC)
    out = _relative_ago(
        past.isoformat().replace("+00:00", "Z"),
        now=now,
    )
    assert "m ago" in out


def test_render_index_html_live_reload_includes_fetch_and_noscript_fallback() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
    }
    html_out = render_index_html(snap, refresh_seconds=10, live_revision="abc")
    assert "/api/revision" in html_out
    assert "fetch(" in html_out
    assert '<noscript><meta http-equiv="refresh" content="10"' in html_out
    assert "  <meta http-equiv=" not in html_out.split("<noscript>")[0]


def test_collect_dashboard_state_includes_purchase_lines(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    paths = DashboardPaths.from_config(cfg)
    paths.state_dir.mkdir(parents=True)
    pj = paths.state_dir / "purchases.jsonl"
    pj.write_text(
        '{"at": "2026-01-01T00:00:00Z", "target": "alpha", "attempt": {"outcome": "ok"}}\n',
        encoding="utf-8",
    )
    snap = collect_dashboard_state(
        DashboardData(cfg=cfg, paths=paths, heartbeat=Heartbeat())
    )
    ph = snap.get("purchases_history") or []
    assert len(ph) == 1
    assert ph[0].get("target") == "alpha"


def test_render_index_html_purchase_panel(tmp_path: Path) -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
        "purchases_history": [
            {
                "at": "2026-01-02T00:00:00Z",
                "target": "t1",
                "attempt": {"outcome": "skipped", "error": None},
            }
        ],
        "paths": {"purchases_jsonl": str(tmp_path / "state" / "purchases.jsonl")},
        "dashboard": {"show_purchase_history": True},
    }
    html_out = render_index_html(snap)
    assert "Purchase history" in html_out
    assert "skipped" in html_out


def test_compute_dashboard_revision_changes_with_purchases_jsonl(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    paths = DashboardPaths.from_config(cfg)
    paths.state_dir.mkdir(parents=True)
    hb = Heartbeat()
    dd = DashboardData(cfg=cfg, paths=paths, heartbeat=hb)
    r1 = compute_dashboard_revision(dd)
    pj = paths.state_dir / "purchases.jsonl"
    pj.write_text('{"x": 1}\n', encoding="utf-8")
    r2 = compute_dashboard_revision(dd)
    assert r1 != r2


def test_compute_dashboard_revision_changes_with_state_mtime(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    paths = DashboardPaths.from_config(cfg)
    paths.state_dir.mkdir(parents=True)
    p = paths.state_dir / "alpha.json"
    p.write_text("{}", encoding="utf-8")
    hb = Heartbeat()
    dd = DashboardData(cfg=cfg, paths=paths, heartbeat=hb)
    r1 = compute_dashboard_revision(dd)
    import time

    time.sleep(0.05)
    p.write_text('{"x": 1}', encoding="utf-8")
    r2 = compute_dashboard_revision(dd)
    assert r1 != r2


def test_render_index_html_disables_meta_refresh_when_zero() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
    }
    html_out = render_index_html(snap, refresh_seconds=0)
    assert 'http-equiv="refresh"' not in html_out
    assert "Auto-refresh off" in html_out


def test_render_index_html_hides_hint_when_state_present() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [
            {
                "name": "t1",
                "url": "https://x",
                "state": {"total_ticks": 1, "current_state": "watching"},
            }
        ],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
    }
    html_out = render_index_html(snap)
    assert "No per-target crawl history yet" not in html_out


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

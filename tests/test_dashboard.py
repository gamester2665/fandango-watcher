"""Tests for src/fandango_watcher/dashboard.py."""

from __future__ import annotations

import json
import re
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
    render_dashboard_not_found_html,
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

    snap = collect_dashboard_state(
        DashboardData(
            cfg=cfg,
            paths=paths,
            heartbeat=hb,
            public_host="127.0.0.1",
            public_port=9999,
        )
    )
    assert snap["runtime"]["public_base_url"] == "http://127.0.0.1:9999/"
    assert snap["runtime"]["host"] == "127.0.0.1"
    assert snap["runtime"]["dashboard_port"] == 9999
    assert snap["runtime"]["purchase_enabled"] is True


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
        "runtime": {"fandango_poll": {"min_seconds": 30, "max_seconds": 35, "error_backoff_cap_seconds": 1800}},
    }
    html_out = render_index_html(snap, refresh_seconds=10, live_revision="abc")
    assert "/api/revision" in html_out
    assert "fetch(" in html_out
    assert "sessionStorage" in html_out
    assert "dash-conn" in html_out
    assert '<noscript><meta http-equiv="refresh" content="10"' in html_out
    assert "  <meta http-equiv=" not in html_out.split("<noscript>")[0]


def test_render_index_html_triage_and_skip_and_zero_targets() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
        "runtime": {
            "fandango_poll": {"min_seconds": 30, "max_seconds": 35, "error_backoff_cap_seconds": 1800},
            "public_base_url": "http://127.0.0.1:8787/",
        },
    }
    html_out = render_index_html(snap)
    assert 'name="color-scheme"' in html_out
    assert "At a glance" in html_out
    assert "Target priority" in html_out
    assert "triage-table" in html_out or "No targets configured" in html_out
    assert 'href="#main"' in html_out
    assert "No <code>targets:</code> in this config" in html_out
    assert 'id="triage"' in html_out


def test_release_intel_empty_dict_operator_copy() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {},
        "movies": [],
    }
    html_out = render_index_html(snap)
    assert "internal" not in html_out
    assert "empty" in html_out.lower() or "payload" in html_out


def test_render_index_html_stable_section_fragment_ids_and_jump_nav() -> None:
    """Anchor targets for in-page navigation and deep links (#triage, #runtime, …)."""
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [{"key": "m1", "title": "M", "fandango_targets": [], "x_handles": []}],
    }
    html_out = render_index_html(snap)
    for fid in ("triage", "runtime", "release-intel", "crawl", "x", "registry"):
        assert f'id="{fid}"' in html_out
    assert 'href="#triage"' in html_out
    assert 'href="#registry"' in html_out
    # Purchase is opt-in via purchases_history + dashboard.show_purchase_history
    assert 'href="#purchase"' not in html_out


def test_target_card_folded_diagnostics_when_content_present() -> None:
    """Diagnostics & media folds errors, staleness text, screenshots, video, traces together."""
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [
            {
                "name": "alpha-show",
                "url": "https://example.com/t",
                "state": {"current_state": "watching", "total_ticks": 2},
                "latest_screenshot_url": "/artifacts/screenshots/alpha-1.png",
            }
        ],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
        "runtime": {"fandango_poll": {"min_seconds": 30, "max_seconds": 35, "error_backoff_cap_seconds": 1800}},
    }
    html_out = render_index_html(snap)
    assert 'data-target="' in html_out
    assert 'class="card-expand"' in html_out
    assert "Diagnostics &amp; media" in html_out


def test_movies_registry_fold_panel_default_collapsed() -> None:
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {"handles": {}},
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [{"key": "k1", "title": "T", "fandango_targets": [], "x_handles": []}],
    }
    html_out = render_index_html(snap)
    assert 'id="registry"' in html_out
    assert re.search(
        r'id="registry"[^>]*>[\s\n]*'
        r'<details class="panel panel-fold"',
        html_out,
    )
    block = html_out.split('id="registry"', 1)[1].split("id=", 1)[0]
    assert '<details class="panel panel-fold" open>' not in block


def test_x_poller_section_prioritizes_snapshot_table_over_detail_cards() -> None:
    """UX: X panel stays scannable by showing the table before folded details."""
    snap = {
        "healthz": {"started_at": "x", "last_tick_at": None, "total_ticks": 0, "total_errors": 0},
        "targets": [],
        "social_x": {
            "last_polled_at": "2026-01-01T00:00:00Z",
            "handles": {
                "testacct": {
                    "handle": "testacct",
                    "user_id": "9",
                    "last_seen_tweet_id": "1",
                    "last_polled_at": "2026-01-01T00:00:00Z",
                    "consecutive_errors": 0,
                }
            },
        },
        "release_intel": {"status": "unconfigured", "reason": "test"},
        "movies": [],
        "runtime": {
            "social_x_poll": {
                "enabled": True,
                "min_seconds": 900,
                "max_seconds": 1200,
                "max_results_per_handle": 10,
                "state_path": "state/social_x.json",
            }
        },
    }
    html_out = render_index_html(snap, refresh_seconds=0)
    assert 'id="x" aria-label="X / Twitter poller">' in html_out
    assert "tweet text (preview)" in html_out
    assert "sx-snapshot" in html_out
    assert "Per-handle details" in html_out
    assert re.search(
        r'id="x"[^>]*>[\s\n]*'
        r'<details class="panel panel-fold" open>',
        html_out,
    )


def test_render_dashboard_not_found_includes_routes() -> None:
    h = render_dashboard_not_found_html(request_path="/nope")
    assert "404" in h
    assert "/nope" in h or "nope" in h
    assert 'href="/"' in h


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

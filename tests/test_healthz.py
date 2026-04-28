"""Tests for src/fandango_watcher/healthz.py.

Spins up the real ``ThreadingHTTPServer`` on an ephemeral port and exercises
the ``/healthz`` endpoint over a plain socket (no httpx/requests dependency).
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from fandango_watcher.dashboard import DashboardData, DashboardPaths
from fandango_watcher.healthz import (
    HealthzContext,
    Heartbeat,
    start_healthz_server,
)


@contextlib.contextmanager
def _running_server(
    hb: Heartbeat,
    *,
    dashboard_data: DashboardData | None = None,
) -> Iterator[HealthzContext]:
    ctx = start_healthz_server(
        hb, host="127.0.0.1", port=0, dashboard_data=dashboard_data
    )
    try:
        yield ctx
    finally:
        ctx.stop()


def _dash_cfg(root: Path) -> WatcherConfig:
    root.mkdir(parents=True, exist_ok=True)
    art = root / "artifacts"
    return WatcherConfig(
        targets=[TargetConfig(name="t1", url="https://example.com/x")],
        theater=TheaterConfig(display_name="CW", fandango_theater_anchor="CW"),
        formats={"require": [], "include": []},  # type: ignore[arg-type]
        poll=PollConfig(min_seconds=30, max_seconds=35),
        purchase=PurchaseConfig(),
        notify=NotifyConfig(channels=[], on_events=[]),
        screenshots=ScreenshotsConfig(
            dir=str(art / "screenshots"),
            per_purchase_dir=str(art / "purchase-attempts"),
        ),
        state=StateConfig(dir=str(root / "state")),
        browser=BrowserConfig(
            user_data_dir=str(root / "profile"),
            record_video_dir=str(art / "videos"),
            record_trace_dir=str(art / "traces"),
            viewport=ViewportConfig(),
        ),
    )


class TestHealthz:
    def test_healthz_returns_200_and_json(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/healthz"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                payload = json.loads(resp.read())

        assert payload["status"] == "ok"
        assert payload["total_ticks"] == 0
        assert payload["total_errors"] == 0
        assert payload["last_tick_at"] is None
        # started_at is ISO-8601; parse round-trips.
        datetime.fromisoformat(payload["started_at"])

    def test_healthz_reflects_heartbeat_updates(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            hb.total_ticks = 7
            hb.total_errors = 2
            hb.last_tick_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
            hb.extra["target"] = "odyssey"

            url = f"http://127.0.0.1:{ctx.port}/healthz"
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read())

        assert payload["total_ticks"] == 7
        assert payload["total_errors"] == 2
        assert payload["last_tick_at"] == "2026-04-16T12:00:00+00:00"
        assert payload["extra"] == {"target": "odyssey"}

    def test_unknown_path_returns_404(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/not-a-real-path"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url, timeout=5)
            assert excinfo.value.code == 404

    def test_unknown_path_with_dashboard_returns_html_404(
        self, tmp_path: Path
    ) -> None:
        cfg = _dash_cfg(tmp_path)
        paths = DashboardPaths.from_config(cfg)
        dd = DashboardData(cfg=cfg, paths=paths, heartbeat=Heartbeat())
        hb = Heartbeat()
        with _running_server(hb, dashboard_data=dd) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/definitely-missing"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url, timeout=5)
            assert excinfo.value.code == 404
            assert excinfo.value.headers.get("Cache-Control") == "no-store"
            assert excinfo.value.headers.get("X-Content-Type-Options") == "nosniff"
            body = excinfo.value.read().decode("utf-8")
        assert "404" in body
        assert "fandango-watcher" in body
        assert 'href="/"' in body

    def test_health_alias_also_returns_200(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/health"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200

    def test_metrics_returns_prometheus_text(self) -> None:
        hb = Heartbeat()
        hb.total_ticks = 3
        hb.total_errors = 1
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/metrics"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200
                assert "text/plain" in resp.headers["Content-Type"]
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                body = resp.read().decode("utf-8")
        assert "fandango_watcher_heartbeat_ticks_total 3" in body
        assert "fandango_watcher_heartbeat_errors_total 1" in body
        assert "# HELP fandango_watcher_heartbeat_ticks_total" in body

    def test_stop_is_idempotent(self) -> None:
        hb = Heartbeat()
        ctx = start_healthz_server(hb, host="127.0.0.1", port=0)
        ctx.stop()
        # Calling stop a second time shouldn't raise; the server is already closed.
        # Python's socketserver tolerates double-close.
        ctx.server.server_close()

    def test_second_bind_on_same_port_raises(self) -> None:
        """Regression: dashboard must NOT silently dual-bind on Windows when
        a stale process is still holding the port. The exclusive subclass
        sets ``allow_reuse_address=False`` so the second start raises
        ``OSError`` immediately instead of round-robining requests between
        two processes serving stale config (orphaned-uv-child bug)."""
        hb = Heartbeat()
        ctx = start_healthz_server(hb, host="127.0.0.1", port=0)
        try:
            with pytest.raises(OSError):
                start_healthz_server(hb, host="127.0.0.1", port=ctx.port)
        finally:
            ctx.stop()


class TestDashboardRoutes:
    def test_root_and_api_status_with_dashboard_data(self, tmp_path: Path) -> None:
        cfg = _dash_cfg(tmp_path)
        paths = DashboardPaths.from_config(cfg)
        dd = DashboardData(cfg=cfg, paths=paths, heartbeat=Heartbeat())
        hb = Heartbeat()
        with _running_server(hb, dashboard_data=dd) as ctx:
            base = f"http://127.0.0.1:{ctx.port}"
            with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
                assert resp.status == 200
                assert "text/html" in resp.headers["Content-Type"]
                assert resp.headers.get("Cache-Control") == "no-store"
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                body = resp.read().decode("utf-8")
                assert "fandango-watcher" in body
                assert "color-scheme" in body
            with urllib.request.urlopen(f"{base}/api/status", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                data = json.loads(resp.read())
                assert "targets" in data
                assert "healthz" in data
            with urllib.request.urlopen(f"{base}/api/revision", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
                rev = json.loads(resp.read())
                assert "revision" in rev
                assert len(rev["revision"]) == 64
            with urllib.request.urlopen(f"{base}/api/purchases", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
                pur = json.loads(resp.read())
                assert "lines" in pur
                assert isinstance(pur["lines"], list)
            with urllib.request.urlopen(f"{base}/api/movies", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
                assert "movies" in json.loads(resp.read())
            with urllib.request.urlopen(f"{base}/api/release_intel", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
                ri = json.loads(resp.read())
                assert ri.get("status") == "unconfigured"

    def test_artifact_file_includes_private_cache(
        self, tmp_path: Path
    ) -> None:
        """Named artifact URLs are typically unique per capture; allow short browser cache."""
        cfg = _dash_cfg(tmp_path)
        paths = DashboardPaths.from_config(cfg)
        shot = paths.screenshot_dir / "ping.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"png")
        dd = DashboardData(cfg=cfg, paths=paths, heartbeat=Heartbeat())
        hb = Heartbeat()
        with _running_server(hb, dashboard_data=dd) as ctx:
            url = (
                f"http://127.0.0.1:{ctx.port}/artifacts/"
                f"screenshots/{shot.name}"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "private, max-age=300"
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_artifacts_path_traversal_returns_404(
        self, tmp_path: Path
    ) -> None:
        cfg = _dash_cfg(tmp_path)
        paths = DashboardPaths.from_config(cfg)
        dd = DashboardData(cfg=cfg, paths=paths, heartbeat=Heartbeat())
        hb = Heartbeat()
        with _running_server(hb, dashboard_data=dd) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/artifacts/../../etc/passwd"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url, timeout=5)
            assert excinfo.value.code == 404

    def test_root_404_without_dashboard_data(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/"
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url, timeout=5)
            assert excinfo.value.code == 404

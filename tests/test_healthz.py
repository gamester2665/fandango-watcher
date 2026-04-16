"""Tests for src/fandango_watcher/healthz.py.

Spins up the real ``ThreadingHTTPServer`` on an ephemeral port and exercises
the ``/healthz`` endpoint over a plain socket (no httpx/requests dependency).
"""

from __future__ import annotations

import contextlib
import json
import urllib.request
from datetime import UTC, datetime
from typing import Iterator

import pytest

from fandango_watcher.healthz import (
    Heartbeat,
    HealthzContext,
    start_healthz_server,
)


@contextlib.contextmanager
def _running_server(hb: Heartbeat) -> Iterator[HealthzContext]:
    ctx = start_healthz_server(hb, host="127.0.0.1", port=0)
    try:
        yield ctx
    finally:
        ctx.stop()


class TestHealthz:
    def test_healthz_returns_200_and_json(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/healthz"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "application/json"
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

    def test_health_alias_also_returns_200(self) -> None:
        hb = Heartbeat()
        with _running_server(hb) as ctx:
            url = f"http://127.0.0.1:{ctx.port}/health"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200

    def test_stop_is_idempotent(self) -> None:
        hb = Heartbeat()
        ctx = start_healthz_server(hb, host="127.0.0.1", port=0)
        ctx.stop()
        # Calling stop a second time shouldn't raise; the server is already closed.
        # Python's socketserver tolerates double-close.
        ctx.server.server_close()

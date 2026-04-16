"""Tiny background HTTP server exposing ``/healthz``.

``docker-compose.yml`` and the Dockerfile both reference
``http://127.0.0.1:8787/healthz`` as the container healthcheck; this module
is the minimal implementation that satisfies that probe without pulling in
an async framework.

The server runs in a daemon thread so a crashed handler can never keep the
process alive. The ``Heartbeat`` dataclass is the shared state; the watch
loop mutates it each tick and the HTTP handler reads a snapshot on request.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


@dataclass
class Heartbeat:
    """Shared mutable state between the watch loop and the healthz server."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_tick_at: datetime | None = None
    total_ticks: int = 0
    total_errors: int = 0
    # Optional free-form details the loop may update (e.g. current target).
    extra: dict[str, object] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "ok",
            "started_at": self.started_at.isoformat(),
            "last_tick_at": (
                self.last_tick_at.isoformat() if self.last_tick_at else None
            ),
            "total_ticks": self.total_ticks,
            "total_errors": self.total_errors,
            "extra": dict(self.extra),
        }


@dataclass
class HealthzContext:
    """Handle returned by :func:`start_healthz_server`; stop via ``stop()``."""

    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def stop(self, timeout: float = 5.0) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=timeout)


def _make_handler_cls(heartbeat: Heartbeat) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log; route it to our logger at
        # DEBUG instead so container stderr isn't spammed by probes.
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug(
                "healthz %s - %s", self.address_string(), format % args
            )

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if self.path in ("/healthz", "/health"):
                payload = json.dumps(heartbeat.snapshot()).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    return Handler


def start_healthz_server(
    heartbeat: Heartbeat,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> HealthzContext:
    """Bind + serve in a daemon thread. Use ``port=0`` in tests for an
    ephemeral port (read it back via ``ctx.port``).
    """
    server = ThreadingHTTPServer((host, port), _make_handler_cls(heartbeat))
    # daemon=True so the process can exit even if ``shutdown()`` is skipped
    # (e.g. during a crash inside the watch loop).
    thread = threading.Thread(
        target=server.serve_forever, name="healthz", daemon=True
    )
    thread.start()
    logger.info(
        "healthz listening on http://%s:%d/healthz",
        host,
        server.server_address[1],
    )
    return HealthzContext(server=server, thread=thread)

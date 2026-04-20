"""Background HTTP server: ``/healthz`` plus optional read-only dashboard."""

from __future__ import annotations

import json
import logging
import mimetypes
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def mutex(self) -> threading.Lock:
        """Use for writes from the watch loop (``with hb.mutex:``)."""
        return self._lock

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
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

    def revision_fingerprint_parts(self) -> list[str]:
        """Atomically read fields used by :func:`~.dashboard.compute_dashboard_revision`."""
        with self._lock:
            parts = [str(self.total_ticks), str(self.total_errors)]
            lt = self.last_tick_at
            parts.append(lt.isoformat() if lt is not None else "")
            extra = self.extra
            if isinstance(extra, dict) and extra:
                parts.append(json.dumps(extra, sort_keys=True, default=str))
            return parts


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


def _send_json(
    handler: BaseHTTPRequestHandler, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _prometheus_metrics_text(heartbeat: Heartbeat) -> bytes:
    """Minimal Prometheus text exposition for the watch-loop heartbeat."""
    snap = heartbeat.snapshot()
    ticks = int(snap.get("total_ticks") or 0)
    errs = int(snap.get("total_errors") or 0)
    extra = snap.get("extra")
    extra_len = len(extra) if isinstance(extra, dict) else 0
    lines = [
        "# HELP fandango_watcher_heartbeat_ticks_total Completed watch loop ticks.",
        "# TYPE fandango_watcher_heartbeat_ticks_total counter",
        f"fandango_watcher_heartbeat_ticks_total {ticks}",
        "# HELP fandango_watcher_heartbeat_errors_total Errors recorded by the watch loop.",
        "# TYPE fandango_watcher_heartbeat_errors_total counter",
        f"fandango_watcher_heartbeat_errors_total {errs}",
        "# HELP fandango_watcher_heartbeat_extra_keys Number of keys in heartbeat.extra.",
        "# TYPE fandango_watcher_heartbeat_extra_keys gauge",
        f"fandango_watcher_heartbeat_extra_keys {extra_len}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _send_bytes(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    content_type: str,
) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _serve_artifact_file(
    handler: BaseHTTPRequestHandler,
    *,
    artifacts_root: Path,
    relative_url_path: str,
) -> bool:
    """Stream a file under ``artifacts_root``. Returns True if handled."""
    rel = relative_url_path.lstrip("/")
    if ".." in rel.split("/"):
        handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        return True
    root = artifacts_root.resolve()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        return True
    if not candidate.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        return True
    mime, _enc = mimetypes.guess_type(str(candidate))
    ctype = mime or "application/octet-stream"
    try:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(candidate.stat().st_size))
        handler.end_headers()
        with candidate.open("rb") as f:
            shutil.copyfileobj(f, handler.wfile)
    except OSError:
        handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
    return True


def _make_handler_cls(
    heartbeat: Heartbeat,
    *,
    dashboard_data: Any | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug(
                "healthz %s - %s", self.address_string(), format % args
            )

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            from .dashboard import (
                collect_dashboard_state,
                compute_dashboard_revision,
                render_index_html,
            )

            parsed = urlparse(self.path)
            path_only = unquote(parsed.path) or "/"

            if path_only in ("/healthz", "/health"):
                payload = json.dumps(heartbeat.snapshot()).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path_only == "/metrics":
                body = _prometheus_metrics_text(heartbeat)
                _send_bytes(self, body, "text/plain; version=0.0.4; charset=utf-8")
                return

            if dashboard_data is not None:
                dd = dashboard_data
                if path_only == "/":
                    snap = collect_dashboard_state(dd)
                    rs = getattr(dd, "refresh_seconds", 10)
                    rev = compute_dashboard_revision(dd)
                    html = render_index_html(
                        snap,
                        refresh_seconds=rs,
                        live_revision=rev,
                    )
                    _send_bytes(self, html.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path_only == "/api/revision":
                    rev = compute_dashboard_revision(dd)
                    _send_json(self, {"revision": rev})
                    return
                if path_only == "/api/status":
                    snap = collect_dashboard_state(dd)
                    _send_json(self, snap)
                    return
                if path_only == "/api/movies":
                    movies = [
                        m.model_dump(mode="json") for m in dd.cfg.movies
                    ]
                    _send_json(self, {"movies": movies})
                    return
                if path_only == "/api/release_intel":
                    from .release_intel import get_release_intel_for_dashboard

                    payload = get_release_intel_for_dashboard(
                        dd.cfg,
                        state_dir=dd.paths.state_dir,
                        settings=dd.settings,
                    )
                    _send_json(self, payload)
                    return
                if path_only.startswith("/artifacts/"):
                    rel = path_only[len("/artifacts/") :]
                    _serve_artifact_file(
                        self,
                        artifacts_root=dd.paths.artifacts_root,
                        relative_url_path=rel,
                    )
                    return

            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    return Handler


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that refuses to share its bind address.

    The stdlib default sets ``allow_reuse_address = True``. On Linux that maps
    to ``SO_REUSEADDR`` (safe — only bypasses TIME_WAIT). On Windows the same
    flag means ``SO_REUSEADDR`` *plus* allowing a **second live listener** on
    the same port; the kernel then round-robins accepts between them. We hit
    that exact bug when a prior ``fandango-watcher dashboard`` left an
    orphaned Python child bound to ``8787`` after its ``uv`` wrapper was
    force-killed: a fresh dashboard happily bound the same port and half the
    requests served stale config. Forcing exclusive bind makes the second
    start fail loudly with ``OSError: [WinError 10048]`` so the operator
    notices instead of staring at a phantom-stale UI.
    """

    allow_reuse_address = False


def start_healthz_server(
    heartbeat: Heartbeat,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    dashboard_data: Any | None = None,
) -> HealthzContext:
    """Bind + serve in a daemon thread. Use ``port=0`` in tests for an
    ephemeral port (read it back via ``ctx.port``).

    When ``dashboard_data`` is set, also serves ``/`` (HTML), ``/api/status``,
    ``/api/revision`` (fingerprint for live tab reload), ``/api/movies``,
    ``/api/release_intel`` (xAI Grok release summaries), and static files under
    ``/artifacts/...``.
    """
    server = _ExclusiveThreadingHTTPServer(
        (host, port),
        _make_handler_cls(heartbeat, dashboard_data=dashboard_data),
    )
    thread = threading.Thread(
        target=server.serve_forever, name="healthz", daemon=True
    )
    thread.start()
    bound = server.server_address[1]
    logger.info(
        "healthz listening on http://%s:%d/healthz",
        host,
        bound,
    )
    if dashboard_data is not None:
        logger.info(
            "dashboard ready: http://%s:%d/  (also /api/status, /api/revision, "
            "/api/movies, /artifacts/)",
            host,
            bound,
        )
    return HealthzContext(server=server, thread=thread)

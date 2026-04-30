"""Background HTTP server: ``/healthz`` plus optional read-only dashboard."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

CITYWALK_THEATER_SLUG = "universal-cinema-amc-at-citywalk-hollywood-aaawx"


def _format_label(tag: Any) -> str:
    from .models import FormatTag

    labels = {
        FormatTag.IMAX_70MM: "IMAX 70MM",
        FormatTag.IMAX: "IMAX",
        FormatTag.THREE_D: "3D",
        FormatTag.SEVENTY_MM: "70MM",
        FormatTag.DOLBY: "Dolby",
        FormatTag.LASER_RECLINER: "Laser Recliner",
        FormatTag.STANDARD: "Standard",
    }
    return labels.get(tag, str(getattr(tag, "value", tag)).replace("_", " "))


def _format_from_slug(format_slug: str) -> tuple[str, Any, str] | None:
    """Resolve URL format slugs to canonical slug, FormatTag, display label."""

    from .detect import normalize_format_label
    from .models import FormatTag

    aliases: dict[str, FormatTag] = {
        "imax": FormatTag.IMAX,
        "imax-70mm": FormatTag.IMAX_70MM,
        "imax70mm": FormatTag.IMAX_70MM,
        "imax_70mm": FormatTag.IMAX_70MM,
        "70mm": FormatTag.SEVENTY_MM,
        "70-mm": FormatTag.SEVENTY_MM,
        "3d": FormatTag.THREE_D,
        "dolby": FormatTag.DOLBY,
        "laser-recliner": FormatTag.LASER_RECLINER,
        "standard": FormatTag.STANDARD,
        "digital": FormatTag.STANDARD,
    }
    raw_slug = format_slug.strip("/").lower()
    tag = aliases.get(raw_slug)
    if tag is None:
        tag = normalize_format_label(raw_slug.replace("-", " "))
        if tag is FormatTag.OTHER:
            return None
    canonical = (
        "imax-70mm"
        if tag is FormatTag.IMAX_70MM
        else tag.value.lower().replace("_", "-")
    )
    return canonical, tag, _format_label(tag)


def _citywalk_format_route(path_only: str) -> tuple[bool, str, str, str, Any, str] | None:
    """Resolve legacy CityWalk and generic theater format routes."""

    from .fandango_api import theater_id_from_slug
    from .models import FormatTag

    if path_only == "/citywalk-imax-70mm":
        return (
            False,
            CITYWALK_THEATER_SLUG,
            "AAAWX",
            "imax-70mm",
            FormatTag.IMAX_70MM,
            "IMAX 70MM",
        )
    if path_only == "/api/citywalk/imax70mm":
        return (
            True,
            CITYWALK_THEATER_SLUG,
            "AAAWX",
            "imax-70mm",
            FormatTag.IMAX_70MM,
            "IMAX 70MM",
        )
    for prefix, is_api in (
        (f"/api/{CITYWALK_THEATER_SLUG}/", True),
        ("/api/citywalk/", True),
        (f"/{CITYWALK_THEATER_SLUG}/", False),
        ("/citywalk/", False),
    ):
        if path_only.startswith(prefix):
            slug = path_only[len(prefix) :].strip("/").lower()
            resolved = _format_from_slug(slug)
            if resolved is None:
                return None
            canonical_slug, tag, label = resolved
            return (is_api, CITYWALK_THEATER_SLUG, "AAAWX", canonical_slug, tag, label)

    segments = [x for x in path_only.strip("/").split("/") if x]
    is_api = False
    if segments and segments[0] == "api":
        is_api = True
        segments = segments[1:]
    if len(segments) != 2:
        return None
    theater_slug, raw_format_slug = segments
    theater_id = theater_id_from_slug(theater_slug)
    if theater_id is None:
        return None
    resolved = _format_from_slug(raw_format_slug)
    if resolved is None:
        return None
    canonical_slug, tag, label = resolved
    return (is_api, theater_slug, theater_id, canonical_slug, tag, label)


def _send_no_sniff(handler: BaseHTTPRequestHandler) -> None:
    """RFC 7034: reduce MIME-sniffing on responses served by this process."""
    handler.send_header("X-Content-Type-Options", "nosniff")


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
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: HTTPStatus = HTTPStatus.OK,
    cache_control: str | None = "no-store",
    send_body: bool = True,
) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    _send_no_sniff(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if send_body:
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
    *,
    status: HTTPStatus = HTTPStatus.OK,
    cache_control: str | None = None,
    send_body: bool = True,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    _send_no_sniff(handler)
    if content_type.split(";")[0].strip().lower() == "text/html":
        handler.send_header(
            "Referrer-Policy",
            "strict-origin-when-cross-origin",
        )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if send_body:
        handler.wfile.write(body)


def _artifact_weak_etag(st: os.stat_result) -> str:
    """RFC 7232 weak entity-tag for conditional GET on static files."""
    return f'W/"{st.st_mtime_ns}-{st.st_size}"'


def _etag_in_if_none_match(if_none_match: str | None, etag: str) -> bool:
    """True if ``If-None-Match`` allows a ``304 Not Modified`` for ``etag``."""
    if not if_none_match:
        return False
    s = if_none_match.strip()
    if s == "*":
        return True
    expected = _weak_etag_opaque_value(etag)
    for part in s.split(","):
        if _weak_etag_opaque_value(part.strip()) == expected:
            return True
    return False


def _weak_etag_opaque_value(value: str) -> str:
    """Opaque tag value for RFC 7232 weak comparison."""
    value = value.strip()
    if value[:2].lower() == "w/":
        value = value[2:].strip()
    return value


def _if_none_match_header_present(if_none_match: str | None) -> bool:
    """RFC 7232: ``If-Modified-Since`` is ignored when this field is sent."""
    return if_none_match is not None and bool(if_none_match.strip())


def _artifact_not_modified_since(
    if_modified_since: str | None,
    *,
    st: os.stat_result,
    if_none_match: str | None,
) -> bool:
    """``True`` when the file is not newer than ``If-Modified-Since`` (GET 304).

    Second-resolution compare; invalid dates are ignored (treat as uncacheable).
    """
    if _if_none_match_header_present(if_none_match):
        return False
    if not if_modified_since or not if_modified_since.strip():
        return False
    try:
        ims_dt = parsedate_to_datetime(if_modified_since.strip())
    except (TypeError, ValueError):
        return False
    if ims_dt.tzinfo is None:
        ims_dt = ims_dt.replace(tzinfo=UTC)
    try:
        ims_sec = int(ims_dt.timestamp())
    except (OSError, OverflowError, ValueError):
        return False
    lm_sec = int(st.st_mtime)
    return lm_sec <= ims_sec


def _send_artifact_validation_headers(
    handler: BaseHTTPRequestHandler,
    *,
    etag: str,
    last_modified: str,
) -> None:
    handler.send_header("Cache-Control", "private, max-age=300")
    handler.send_header("ETag", etag)
    handler.send_header("Last-Modified", last_modified)
    handler.send_header("Accept-Ranges", "none")
    _send_no_sniff(handler)


def _send_artifact_range_not_satisfiable(
    handler: BaseHTTPRequestHandler,
    *,
    etag: str,
    last_modified: str,
    size: int,
) -> None:
    handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
    _send_artifact_validation_headers(
        handler, etag=etag, last_modified=last_modified
    )
    handler.send_header("Content-Range", f"bytes */{size}")
    handler.end_headers()


def _send_artifact_not_found(
    handler: BaseHTTPRequestHandler,
    *,
    send_body: bool,
) -> None:
    body = b"Not Found\n"
    handler.send_response(HTTPStatus.NOT_FOUND)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    _send_no_sniff(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if send_body:
        handler.wfile.write(body)


def _serve_artifact_file(
    handler: BaseHTTPRequestHandler,
    *,
    artifacts_root: Path,
    relative_url_path: str,
    send_body: bool = True,
) -> bool:
    """Stream a file under ``artifacts_root``. Returns True if handled.

    When ``send_body`` is false (typically ``HEAD``), response headers match
    ``GET`` but no bytes follow (RFC 7231).
    """
    rel = relative_url_path.lstrip("/")
    if ".." in rel.split("/"):
        _send_artifact_not_found(handler, send_body=send_body)
        return True
    root = artifacts_root.resolve()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        _send_artifact_not_found(handler, send_body=send_body)
        return True
    if not candidate.is_file():
        _send_artifact_not_found(handler, send_body=send_body)
        return True
    mime, _enc = mimetypes.guess_type(str(candidate))
    ctype = mime or "application/octet-stream"
    try:
        st = candidate.stat()
        etag = _artifact_weak_etag(st)
        last_modified = formatdate(st.st_mtime, usegmt=True)
        inm = handler.headers.get("If-None-Match")
        if _etag_in_if_none_match(inm, etag):
            handler.send_response(HTTPStatus.NOT_MODIFIED)
            _send_artifact_validation_headers(
                handler, etag=etag, last_modified=last_modified
            )
            handler.end_headers()
            return True
        if _artifact_not_modified_since(
            handler.headers.get("If-Modified-Since"),
            st=st,
            if_none_match=inm,
        ):
            handler.send_response(HTTPStatus.NOT_MODIFIED)
            _send_artifact_validation_headers(
                handler, etag=etag, last_modified=last_modified
            )
            handler.end_headers()
            return True
        if handler.headers.get("Range"):
            _send_artifact_range_not_satisfiable(
                handler,
                etag=etag,
                last_modified=last_modified,
                size=st.st_size,
            )
            return True
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", ctype)
        _send_artifact_validation_headers(
            handler, etag=etag, last_modified=last_modified
        )
        handler.send_header("Content-Length", str(st.st_size))
        handler.end_headers()
        if send_body:
            with candidate.open("rb") as f:
                shutil.copyfileobj(f, handler.wfile)
    except OSError:
        _send_artifact_not_found(handler, send_body=send_body)
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

        def _handle_readonly_request(self, *, send_body: bool) -> None:
            from .dashboard import (
                collect_dashboard_state,
                compute_dashboard_revision,
                render_index_html,
            )

            parsed = urlparse(self.path)
            path_only = unquote(parsed.path) or "/"

            if path_only in ("/healthz", "/health"):
                _send_json(self, heartbeat.snapshot(), send_body=send_body)
                return

            if path_only == "/metrics":
                body = _prometheus_metrics_text(heartbeat)
                _send_bytes(
                    self,
                    body,
                    "text/plain; version=0.0.4; charset=utf-8",
                    cache_control="no-store",
                    send_body=send_body,
                )
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
                    _send_bytes(
                        self,
                        html.encode("utf-8"),
                        "text/html; charset=utf-8",
                        cache_control="no-store",
                        send_body=send_body,
                    )
                    return
                if path_only == "/api/revision":
                    rev = compute_dashboard_revision(dd)
                    _send_json(self, {"revision": rev}, send_body=send_body)
                    return
                if path_only == "/api/status":
                    snap = collect_dashboard_state(dd)
                    _send_json(self, snap, send_body=send_body)
                    return
                if path_only == "/api/purchases":
                    snap = collect_dashboard_state(dd)
                    _send_json(
                        self,
                        {
                            "lines": snap.get("purchases_history") or [],
                            "path": snap.get("paths", {}).get("purchases_jsonl"),
                            "dashboard": snap.get("dashboard") or {},
                        },
                        send_body=send_body,
                    )
                    return
                if path_only == "/api/movies":
                    movies = [
                        m.model_dump(mode="json") for m in dd.cfg.movies
                    ]
                    _send_json(self, {"movies": movies}, send_body=send_body)
                    return
                if path_only == "/api/fandango/search":
                    query = (parse_qs(parsed.query).get("q") or [""])[0].strip()
                    if not query:
                        _send_json(
                            self,
                            {"ok": False, "error": "missing q query parameter"},
                            status=HTTPStatus.BAD_REQUEST,
                            send_body=send_body,
                        )
                        return
                    from .fandango_api import FandangoApiClient

                    try:
                        with FandangoApiClient(
                            base_url=dd.cfg.direct_api.base_url,
                            theater_id=dd.cfg.direct_api.theater_id,
                            chain_code=dd.cfg.direct_api.chain_code,
                        ) as client:
                            results = [
                                item.model_dump(mode="json")
                                for item in client.search_movies(query)
                            ]
                    except Exception as exc:  # noqa: BLE001 - dashboard endpoint should return JSON
                        logger.warning("Fandango search failed query=%r", query, exc_info=True)
                        _send_json(
                            self,
                            {"ok": False, "error": f"Fandango search failed: {type(exc).__name__}"},
                            status=HTTPStatus.BAD_GATEWAY,
                            send_body=send_body,
                        )
                        return
                    _send_json(
                        self,
                        {"ok": True, "query": query, "results": results},
                        send_body=send_body,
                    )
                    return
                citywalk_format = _citywalk_format_route(path_only)
                if citywalk_format is not None:
                    from .fandango_api import FandangoApiClient

                    (
                        is_api,
                        theater_slug,
                        theater_id,
                        format_slug,
                        format_tag,
                        format_label,
                    ) = citywalk_format

                    try:
                        with FandangoApiClient(
                            base_url=dd.cfg.direct_api.base_url,
                            theater_id=dd.cfg.direct_api.theater_id,
                            chain_code=dd.cfg.direct_api.chain_code,
                        ) as client:
                            theater_info = client.theater_info(theater_slug)
                            by_date = client.future_format_records(
                                {format_tag},
                                theater_id=theater_info.theater_id,
                                chain_code=theater_info.chain_code,
                            )
                        dates = [
                            {
                                "date": showtime_date,
                                "showtimes": [
                                    record.model_dump(mode="json")
                                    for record in records
                                ],
                            }
                            for showtime_date, records in by_date.items()
                        ]
                        payload = {
                            "ok": True,
                            "source": "fandango_theater_calendar",
                            "watchlist_filtered": False,
                            "theater_slug": theater_info.slug,
                            "theater_id": theater_info.theater_id,
                            "theater_name": theater_info.name,
                            "chain_code": theater_info.chain_code,
                            "format": format_tag.value,
                            "format_slug": format_slug,
                            "format_label": format_label,
                            "generated_at": datetime.now(UTC).isoformat(),
                            "date_count": len(dates),
                            "showtime_count": sum(len(day["showtimes"]) for day in dates),
                            "dates": dates,
                        }
                    except Exception as exc:  # noqa: BLE001 - route should remain JSON/HTML-shaped
                        logger.warning(
                            "Fandango %s/%s lookup failed",
                            theater_slug,
                            format_slug,
                            exc_info=True,
                        )
                        payload = {
                            "ok": False,
                            "error": f"Fandango lookup failed: {type(exc).__name__}",
                            "source": "fandango_theater_calendar",
                            "watchlist_filtered": False,
                            "theater_slug": theater_slug,
                            "theater_id": theater_id,
                            "theater_name": None,
                            "chain_code": dd.cfg.direct_api.chain_code,
                            "format": format_tag.value,
                            "format_slug": format_slug,
                            "format_label": format_label,
                            "generated_at": datetime.now(UTC).isoformat(),
                            "date_count": 0,
                            "showtime_count": 0,
                            "dates": [],
                        }
                    if is_api:
                        status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_GATEWAY
                        _send_json(self, payload, status=status, send_body=send_body)
                        return
                    from .dashboard import render_citywalk_format_html

                    html = render_citywalk_format_html(payload)
                    status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_GATEWAY
                    _send_bytes(
                        self,
                        html.encode("utf-8"),
                        "text/html; charset=utf-8",
                        cache_control="no-store",
                        send_body=send_body,
                        status=status,
                    )
                    return
                if path_only == "/api/release_intel":
                    from .release_intel import get_release_intel_for_dashboard

                    payload = get_release_intel_for_dashboard(
                        dd.cfg,
                        state_dir=dd.paths.state_dir,
                        settings=dd.settings,
                    )
                    _send_json(self, payload, send_body=send_body)
                    return
                if path_only.startswith("/artifacts/"):
                    rel = path_only[len("/artifacts/") :]
                    _serve_artifact_file(
                        self,
                        artifacts_root=dd.paths.artifacts_root,
                        relative_url_path=rel,
                        send_body=send_body,
                    )
                    return

                from .dashboard import render_dashboard_not_found_html

                body = render_dashboard_not_found_html(
                    request_path=path_only
                ).encode("utf-8")
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                _send_no_sniff(self)
                self.send_header(
                    "Referrer-Policy",
                    "strict-origin-when-cross-origin",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            self._handle_readonly_request(send_body=True)

        def do_HEAD(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            """RFC 7231: same headers as GET for read-only routes; no payload."""
            self._handle_readonly_request(send_body=False)

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            path_only = unquote(parsed.path)
            if dashboard_data is None or path_only != "/api/movies/add":
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
                return
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = 0
            if n <= 0 or n > 100_000:
                _send_json(
                    self,
                    {"ok": False, "error": "invalid JSON body length"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                _send_json(
                    self,
                    {"ok": False, "error": "invalid JSON body"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if not isinstance(payload, dict):
                _send_json(
                    self,
                    {"ok": False, "error": "JSON body must be an object"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            from .dashboard import add_movie_from_fandango_search_result

            try:
                result = add_movie_from_fandango_search_result(dashboard_data, payload)
            except ValueError as exc:
                _send_json(
                    self,
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            except Exception as exc:  # noqa: BLE001 - keep dashboard POST JSON-shaped
                logger.warning("failed to add movie from dashboard", exc_info=True)
                _send_json(
                    self,
                    {"ok": False, "error": f"failed to update config: {type(exc).__name__}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            _send_json(self, {"ok": True, **result})

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
    ``/api/revision`` (fingerprint for live tab reload), ``/api/purchases``,
    ``/api/movies``, ``/api/release_intel`` (xAI Grok release summaries), static
    files under ``/artifacts/...``, and ``HEAD`` on artifact URLs (same headers
    as ``GET``, no body).
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
            "/api/purchases, /api/movies, /artifacts/)",
            host,
            bound,
        )
    return HealthzContext(server=server, thread=thread)

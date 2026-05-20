"""Sync HTTP handlers for the local SQLite watchlist config API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .cloudflare_config import ConfigConflictError, MoviePatch
from .config import MovieConfig, TargetConfig, plain_secret
from .sqlite_watchlist import SqliteWatchlistProvider
from .watchlist_ops import build_movie_add_plan

logger = logging.getLogger(__name__)


def _read_json_body(body: bytes) -> dict[str, Any]:
    if not body.strip():
        return {}
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _expected_revision(payload: dict[str, Any]) -> int | None:
    value = payload.get("expected_revision")
    if value is None:
        return None
    return int(value)


def _require_admin(auth_header: str | None, admin_token: str) -> bool:
    expected = plain_secret(admin_token).strip()
    if not expected:
        return False
    return (auth_header or "") == f"Bearer {expected}"


def _error(code: str, message: str, *, status: int) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error": {"code": code, "message": message}}


def handle_local_config_request(
    method: str,
    path: str,
    body: bytes,
    *,
    admin_token: str,
    db_path: str,
    auth_header: str | None = None,
) -> tuple[int, dict[str, Any]]:
    path = path.rstrip("/") or "/"
    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()

    if path == "/api/watchlist/revision" and method == "GET":
        return 200, {"revision": provider.get_revision()}

    if path == "/api/watchlist" and method == "GET":
        return 200, provider.get_watchlist()

    auth = None

    if path == "/api/watchlist/replace" and method == "POST":
        try:
            payload = _read_json_body(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return _error("invalid_request", str(exc), status=400)
        if not _require_admin(auth_header, admin_token):
            return _error("unauthorized", "missing or invalid admin token", status=401)
        try:
            targets = [TargetConfig.model_validate(t) for t in payload.get("targets") or []]
            movies = [MovieConfig.model_validate(m) for m in payload.get("movies") or []]
            result = provider.replace_watchlist(
                targets,
                movies,
                force=bool(payload.get("force")),
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return _error("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("watchlist replace failed")
            return _error("invalid_request", str(exc), status=400)
        return 200, {"ok": True, **result}

    if path.startswith("/api/movies"):
        return _handle_movies(method, path, body, provider, admin_token, auth_header)

    return _error("not_found", "route not found", status=404)


def _handle_movies(
    method: str,
    path: str,
    body: bytes,
    provider: SqliteWatchlistProvider,
    admin_token: str,
    auth_header: str | None,
) -> tuple[int, dict[str, Any]]:
    if method == "POST" and path == "/api/movies":
        if not _require_admin(auth_header, admin_token):
            return _error("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = _read_json_body(body)
            watchlist = provider.get_watchlist()
            existing_targets = {t["name"] for t in watchlist.get("targets") or []}
            existing_movies = {m["key"] for m in watchlist.get("movies") or []}
            movie, targets = build_movie_add_plan(
                payload,
                existing_target_names=existing_targets,
                existing_movie_keys=existing_movies,
            )
            result = provider.upsert_movie_with_targets(
                movie,
                targets,
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return _error("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("create movie failed")
            return _error("invalid_request", str(exc), status=400)
        return 200, {
            "ok": True,
            "movie": movie.model_dump(mode="json"),
            "targets": [{"name": t.name, "url": t.url} for t in targets],
            "restart_watch_required": False,
            **result,
        }

    match = re.match(r"^/api/movies/([^/]+)$", path)
    if not match:
        return _error("not_found", "route not found", status=404)
    key = match.group(1)

    if method == "PATCH":
        if not _require_admin(auth_header, admin_token):
            return _error("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = _read_json_body(body)
            patch = MoviePatch.model_validate(payload)
            result = provider.patch_movie(
                key,
                patch,
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return _error("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("patch movie failed")
            return _error("invalid_request", str(exc), status=400)
        return 200, {"ok": True, **result}

    if method == "DELETE":
        if not _require_admin(auth_header, admin_token):
            return _error("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = _read_json_body(body)
            result = provider.delete_movie(
                key,
                delete_owned_targets=bool(payload.get("delete_owned_targets", True)),
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return _error("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("delete movie failed")
            return _error("invalid_request", str(exc), status=400)
        return 200, {"ok": True, **result}

    return _error("method_not_allowed", f"{method} not allowed", status=405)

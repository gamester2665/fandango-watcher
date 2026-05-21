"""HTTP routing for the Cloudflare Worker watchlist config API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from worker_d1_config import ConfigConflictError, D1WatchlistProvider
from worker_watchlist_ops import build_movie_add_plan

logger = logging.getLogger(__name__)


def json_response(payload: dict[str, Any], *, status: int = 200):
    from workers import Response

    return Response(
        json.dumps(payload),
        status=status,
        headers={
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
    )


def error_response(code: str, message: str, *, status: int = 400):
    return json_response({"ok": False, "error": {"code": code, "message": message}}, status=status)


def require_admin(request, env) -> bool:
    expected = getattr(env, "CONFIG_ADMIN_TOKEN", "") or ""
    if not expected:
        return False
    auth = request.headers.get("Authorization") or ""
    return auth == f"Bearer {expected}"


async def _read_json_body(request) -> dict[str, Any]:
    try:
        raw = await request.text()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _expected_revision(payload: dict[str, Any]) -> int | None:
    value = payload.get("expected_revision")
    if value is None:
        return None
    return int(value)


def _normalize_target_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("target must be an object")
    name = raw.get("name")
    url = raw.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("target.name is required")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("target.url is required")
    return {"name": name.strip(), "url": url.strip(), **raw}


def _normalize_movie_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("movie must be an object")
    key = raw.get("key")
    title = raw.get("title")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("movie.key is required")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("movie.title is required")
    return {"key": key.strip(), "title": title.strip(), **raw}


def _normalize_patch_dict(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "title",
        "release_date",
        "poster_url",
        "preferred_formats",
        "x_handles",
        "x_keywords",
        "distributor",
        "reference_page_key",
    }
    unknown = set(raw) - allowed - {"expected_revision"}
    if unknown:
        raise ValueError(f"unsupported patch fields: {sorted(unknown)}")
    return {k: v for k, v in raw.items() if k in allowed}


async def handle_config_fetch(request, env) -> Any:
    url = urlparse(str(request.url))
    path = url.path.rstrip("/") or "/"
    method = request.method

    if path == "/healthz":
        return json_response({"ok": True})

    provider = D1WatchlistProvider(env.DB)
    await provider.init_schema()

    if path == "/api/watchlist/revision" and method == "GET":
        revision = await provider.get_revision()
        return json_response({"revision": revision})

    if path == "/api/watchlist" and method == "GET":
        data = await provider.get_watchlist()
        return json_response(data)

    if path == "/api/watchlist/replace" and method == "POST":
        if not require_admin(request, env):
            return error_response("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = await _read_json_body(request)
            targets = [_normalize_target_dict(t) for t in payload.get("targets") or []]
            movies = [_normalize_movie_dict(m) for m in payload.get("movies") or []]
            result = await provider.replace_watchlist(
                targets,
                movies,
                force=bool(payload.get("force")),
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return error_response("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("watchlist replace failed")
            return error_response("invalid_request", str(exc), status=400)
        return json_response({"ok": True, **result})

    if path.startswith("/api/movies"):
        return await _handle_movies_crud(request, env, provider, path, method)

    return error_response("not_found", "route not found", status=404)


async def _handle_movies_crud(request, env, provider: D1WatchlistProvider, path: str, method: str) -> Any:
    if method == "POST" and path == "/api/movies":
        if not require_admin(request, env):
            return error_response("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = await _read_json_body(request)
            watchlist = await provider.get_watchlist()
            existing_targets = {t["name"] for t in watchlist.get("targets") or []}
            existing_movies = {m["key"] for m in watchlist.get("movies") or []}
            movie, targets = build_movie_add_plan(
                payload,
                existing_target_names=existing_targets,
                existing_movie_keys=existing_movies,
            )
            result = await provider.upsert_movie_with_targets(
                movie,
                targets,
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return error_response("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("create movie failed")
            return error_response("invalid_request", str(exc), status=400)
        return json_response(
            {
                "ok": True,
                "movie": movie,
                "targets": [{"name": t["name"], "url": t["url"]} for t in targets],
                "restart_watch_required": False,
                **result,
            }
        )

    match = re.match(r"^/api/movies/([^/]+)$", path)
    if not match:
        return error_response("not_found", "route not found", status=404)
    key = match.group(1)

    if method == "PATCH":
        if not require_admin(request, env):
            return error_response("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = await _read_json_body(request)
            patch = _normalize_patch_dict(payload)
            result = await provider.patch_movie(
                key,
                patch,
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return error_response("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("patch movie failed")
            return error_response("invalid_request", str(exc), status=400)
        return json_response({"ok": True, **result})

    if method == "DELETE":
        if not require_admin(request, env):
            return error_response("unauthorized", "missing or invalid admin token", status=401)
        try:
            payload = await _read_json_body(request)
            result = await provider.delete_movie(
                key,
                delete_owned_targets=bool(payload.get("delete_owned_targets", True)),
                expected_revision=_expected_revision(payload),
            )
        except ConfigConflictError as exc:
            return error_response("conflict", str(exc), status=409)
        except Exception as exc:
            logger.exception("delete movie failed")
            return error_response("invalid_request", str(exc), status=400)
        return json_response({"ok": True, **result})

    return error_response("method_not_allowed", f"{method} not allowed", status=405)

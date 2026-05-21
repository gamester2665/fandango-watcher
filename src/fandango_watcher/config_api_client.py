"""HTTP client for the Cloudflare Worker watchlist config API."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .config import (
    MovieConfig,
    RemoteWatchlist,
    Settings,
    TargetConfig,
    WatcherConfig,
    load_config,
    merge_watchlist,
    plain_secret,
)

logger = logging.getLogger(__name__)


class ConfigApiError(Exception):
    """Raised when the remote config API returns an error response."""


def _remote_config_api_url(settings: Settings) -> str:
    return settings.config_api_url.strip()


def _local_config_db_path(settings: Settings) -> str:
    return settings.config_local_db_path.strip()


def watchlist_backend_configured(settings: Settings) -> bool:
    return bool(_remote_config_api_url(settings) or _local_config_db_path(settings))


def config_writes_enabled(settings: Settings) -> bool:
    return watchlist_backend_configured(settings) and bool(
        plain_secret(settings.config_admin_token).strip()
    )


def watchlist_config_source(settings: Settings) -> str:
    if _remote_config_api_url(settings):
        return "d1"
    if _local_config_db_path(settings):
        return "sqlite"
    return "yaml"


def fetch_watchlist_local(db_path: str) -> RemoteWatchlist:
    from .sqlite_watchlist import SqliteWatchlistProvider

    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()
    return RemoteWatchlist.model_validate(provider.get_watchlist())


def fetch_revision_local(db_path: str) -> int:
    from .sqlite_watchlist import SqliteWatchlistProvider

    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()
    return provider.get_revision()


def fetch_watchlist_for_settings(settings: Settings) -> RemoteWatchlist:
    api_url = _remote_config_api_url(settings)
    if api_url:
        return fetch_watchlist_http(api_url)
    db_path = _local_config_db_path(settings)
    if db_path:
        return fetch_watchlist_local(db_path)
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required")


def fetch_revision_for_settings(settings: Settings) -> int:
    api_url = _remote_config_api_url(settings)
    if api_url:
        return fetch_revision_http(api_url)
    db_path = _local_config_db_path(settings)
    if db_path:
        return fetch_revision_local(db_path)
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required")


def fetch_watchlist_http(base_url: str, *, timeout: float = 15.0) -> RemoteWatchlist:
    url = f"{base_url.rstrip('/')}/api/watchlist"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
    return RemoteWatchlist.model_validate(resp.json())


def fetch_revision_http(base_url: str, *, timeout: float = 5.0) -> int:
    url = f"{base_url.rstrip('/')}/api/watchlist/revision"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
    data = resp.json()
    return int(data["revision"])


def proxy_admin_request(
    method: str,
    path: str,
    body: bytes,
    settings: Settings,
    *,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    base = settings.config_api_url.rstrip("/")
    token = plain_secret(settings.config_admin_token)
    if not base or not token:
        raise ConfigApiError("CONFIG_API_URL and CONFIG_ADMIN_TOKEN are required for admin writes")
    url = f"{base}{path}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(
            method,
            url,
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
    return resp.status_code, resp.content


def admin_json_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    settings: Settings,
) -> dict[str, Any]:
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    status, raw = proxy_admin_request(method, path, body, settings)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise ConfigApiError(f"invalid JSON from config API ({status})") from exc
    if status >= 400 or data.get("ok") is False:
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or str(err)
        else:
            message = str(err or f"HTTP {status}")
        raise ConfigApiError(message)
    return data


def write_watchlist_cache(cache_path: str | Path, remote: RemoteWatchlist, *, source: str) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat(),
        "watchlist": remote.model_dump(mode="json"),
    }
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_watchlist_cache(cache_path: str | Path) -> tuple[RemoteWatchlist, dict[str, Any]]:
    path = Path(cache_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "watchlist" not in raw:
        raise ValueError(f"invalid watchlist cache file: {path}")
    watchlist = RemoteWatchlist.model_validate(raw["watchlist"])
    return watchlist, raw


def cache_age_seconds(cache_meta: dict[str, Any]) -> int | None:
    fetched_at = cache_meta.get("fetched_at")
    if not isinstance(fetched_at, str):
        return None
    try:
        dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - dt.astimezone(UTC)).total_seconds()))


def load_config_merged(path: str | Path, settings: Settings) -> tuple[WatcherConfig, int | None, dict[str, Any]]:
    """Load YAML policy and optionally overlay D1 watchlist from Worker API."""
    base = load_config(path)
    meta: dict[str, Any] = {
        "config_source": "yaml",
        "config_revision": None,
        "config_cache_age_seconds": None,
    }
    if not watchlist_backend_configured(settings):
        return base, None, meta

    api_url = _remote_config_api_url(settings)
    source = watchlist_config_source(settings)
    cache_path = Path(settings.config_cache_path)
    try:
        remote = fetch_watchlist_for_settings(settings)
        if (
            remote.revision == 0
            and not remote.targets
            and not remote.movies
        ):
            meta["config_source"] = "yaml"
            return base, None, meta
        write_watchlist_cache(cache_path, remote, source=api_url or "local-sqlite")
        merged = merge_watchlist(base, remote.targets, remote.movies)
        meta.update(
            {
                "config_source": source,
                "config_revision": remote.revision,
                "config_cache_age_seconds": 0,
            }
        )
        return merged, remote.revision, meta
    except Exception as exc:
        if cache_path.is_file():
            remote, cache_meta = read_watchlist_cache(cache_path)
            logger.warning(
                "config API fetch failed (%s); using cached watchlist revision=%s age=%ss",
                exc,
                remote.revision,
                cache_age_seconds(cache_meta),
            )
            merged = merge_watchlist(base, remote.targets, remote.movies)
            meta.update(
                {
                    "config_source": f"{source}-cache",
                    "config_revision": remote.revision,
                    "config_cache_age_seconds": cache_age_seconds(cache_meta),
                }
            )
            return merged, remote.revision, meta
        raise RuntimeError(
            f"CONFIG_API_URL is set but watchlist fetch failed and no cache exists at {cache_path}: {exc}"
        ) from exc


def reload_merged_config(
    policy_path: Path,
    settings: Settings,
    policy_cfg: WatcherConfig | None = None,
) -> tuple[WatcherConfig, int | None, dict[str, Any]]:
    policy = policy_cfg if policy_cfg is not None else load_config(policy_path)
    if not watchlist_backend_configured(settings):
        return policy, None, {"config_source": "yaml", "config_revision": None}
    api_url = _remote_config_api_url(settings)
    remote = fetch_watchlist_for_settings(settings)
    write_watchlist_cache(settings.config_cache_path, remote, source=api_url or "local-sqlite")
    merged = merge_watchlist(policy, remote.targets, remote.movies)
    meta = {
        "config_source": watchlist_config_source(settings),
        "config_revision": remote.revision,
        "config_cache_age_seconds": 0,
    }
    return merged, remote.revision, meta


def _ensure_poster_in_payload(payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("title")
    url = payload.get("url")
    if not isinstance(title, str) or not isinstance(url, str):
        return payload
    poster = payload.get("poster_url")
    if isinstance(poster, str) and poster.strip():
        return payload
    from .fandango_api import resolve_movie_poster_url

    poster_url = resolve_movie_poster_url(title, url)
    if not poster_url:
        return payload
    enriched = dict(payload)
    enriched["poster_url"] = poster_url
    return enriched


def remote_add_movie(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    payload = _ensure_poster_in_payload(payload)
    api_url = _remote_config_api_url(settings)
    if api_url:
        return admin_json_request("POST", "/api/movies", payload, settings)
    db_path = _local_config_db_path(settings)
    if db_path:
        return _local_add_movie(db_path, payload)
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required for movie add")


def remote_patch_movie(settings: Settings, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_url = _remote_config_api_url(settings)
    if api_url:
        return admin_json_request("PATCH", f"/api/movies/{key}", payload, settings)
    db_path = _local_config_db_path(settings)
    if db_path:
        return _local_patch_movie(db_path, key, payload)
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required for movie patch")


def remote_delete_movie(settings: Settings, key: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    api_url = _remote_config_api_url(settings)
    if api_url:
        return admin_json_request("DELETE", f"/api/movies/{key}", payload or None, settings)
    db_path = _local_config_db_path(settings)
    if db_path:
        return _local_delete_movie(db_path, key, payload)
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required for movie delete")


def remote_replace_watchlist(
    settings: Settings,
    targets: list[TargetConfig],
    movies: list[MovieConfig],
    *,
    force: bool = False,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "targets": [t.model_dump(mode="json") for t in targets],
        "movies": [m.model_dump(mode="json") for m in movies],
        "force": force,
    }
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    api_url = _remote_config_api_url(settings)
    if api_url:
        return admin_json_request("POST", "/api/watchlist/replace", payload, settings)
    db_path = _local_config_db_path(settings)
    if db_path:
        from .sqlite_watchlist import SqliteWatchlistProvider

        provider = SqliteWatchlistProvider(db_path)
        provider.init_schema()
        result = provider.replace_watchlist(
            targets,
            movies,
            force=force,
            expected_revision=expected_revision,
        )
        return {"ok": True, **result}
    raise ConfigApiError("CONFIG_API_URL or CONFIG_LOCAL_DB_PATH is required for watchlist replace")


def _expected_revision(payload: dict[str, Any]) -> int | None:
    value = payload.get("expected_revision")
    if value is None:
        return None
    return int(value)


def _local_add_movie(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .sqlite_watchlist import SqliteWatchlistProvider
    from .watchlist_ops import build_movie_add_plan

    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()
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
    return {
        "ok": True,
        "movie": movie.model_dump(mode="json"),
        "targets": [{"name": t.name, "url": t.url} for t in targets],
        "restart_watch_required": False,
        **result,
    }


def _local_patch_movie(db_path: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .cloudflare_config import MoviePatch
    from .sqlite_watchlist import SqliteWatchlistProvider

    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()
    patch = MoviePatch.model_validate(payload)
    result = provider.patch_movie(
        key,
        patch,
        expected_revision=_expected_revision(payload),
    )
    return {"ok": True, **result}


def _local_delete_movie(db_path: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .sqlite_watchlist import SqliteWatchlistProvider

    provider = SqliteWatchlistProvider(db_path)
    provider.init_schema()
    result = provider.delete_movie(
        key,
        delete_owned_targets=bool(payload.get("delete_owned_targets", True)),
        expected_revision=_expected_revision(payload),
    )
    return {"ok": True, **result}


def export_watchlist_yaml(targets: list[TargetConfig], movies: list[MovieConfig]) -> str:
    import yaml

    return yaml.safe_dump(
        {
            "targets": [t.model_dump(mode="python") for t in targets],
            "movies": [m.model_dump(mode="python") for m in movies],
        },
        sort_keys=False,
        allow_unicode=True,
    )

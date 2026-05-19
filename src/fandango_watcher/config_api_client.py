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


def config_writes_enabled(settings: Settings) -> bool:
    return bool(settings.config_api_url.strip() and plain_secret(settings.config_admin_token).strip())


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
    api_url = settings.config_api_url.strip()
    if not api_url:
        return base, None, meta

    cache_path = Path(settings.config_cache_path)
    try:
        remote = fetch_watchlist_http(api_url)
        write_watchlist_cache(cache_path, remote, source=api_url)
        merged = merge_watchlist(base, remote.targets, remote.movies)
        meta.update(
            {
                "config_source": "d1",
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
                    "config_source": "d1-cache",
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
    api_url = settings.config_api_url.strip()
    if not api_url:
        return policy, None, {"config_source": "yaml", "config_revision": None}
    remote = fetch_watchlist_http(api_url)
    write_watchlist_cache(settings.config_cache_path, remote, source=api_url)
    merged = merge_watchlist(policy, remote.targets, remote.movies)
    meta = {
        "config_source": "d1",
        "config_revision": remote.revision,
        "config_cache_age_seconds": 0,
    }
    return merged, remote.revision, meta


def remote_add_movie(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    return admin_json_request("POST", "/api/movies", payload, settings)


def remote_patch_movie(settings: Settings, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    return admin_json_request("PATCH", f"/api/movies/{key}", payload, settings)


def remote_delete_movie(settings: Settings, key: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    return admin_json_request("DELETE", f"/api/movies/{key}", payload or None, settings)


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
    return admin_json_request("POST", "/api/watchlist/replace", payload, settings)


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

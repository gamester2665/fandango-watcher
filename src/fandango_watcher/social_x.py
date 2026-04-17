"""Phase 2.5 — X (Twitter) social-signal poller.

This module is **advisory only**: it watches a small set of official movie
or studio X accounts for early "tickets soon" copy and emits soft hint
notifications. It is intentionally decoupled from the Fandango watcher:

* Separate poll cadence (``social_x.min_seconds`` / ``max_seconds``).
* Separate state file (``state/social_x.json``).
* Failures here NEVER affect the Fandango tick — caller catches and logs.

Fandango remains the single source of truth for
``release_transition_bad_to_good``. ``social_x_match`` is a hint, not a
buy signal.

API surface used (X API v2):
* ``GET /2/users/by/username/{handle}`` — resolve handle -> user id once
  per handle, then cached forever in state.
* ``GET /2/users/{id}/tweets`` — paged recent tweets, filtered by
  ``since_id`` so we only see what's new since the last poll.

Auth: Bearer token only (read-only public-tweet endpoints). The OAuth1
key/secret pair is unused here but kept in ``Settings`` for a future
posting / user-context flow.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import SocialXConfig, SocialXHandleConfig

logger = logging.getLogger(__name__)


X_API_BASE = "https://api.x.com/2"
DEFAULT_TIMEOUT_SECONDS = 15.0


# -----------------------------------------------------------------------------
# State persistence
# -----------------------------------------------------------------------------


class HandleState(BaseModel):
    """Per-handle cache so we (a) only resolve user_id once and (b) only
    fetch tweets newer than the last one we already evaluated.
    """

    model_config = ConfigDict(extra="forbid")

    handle: str
    user_id: str | None = None
    last_seen_tweet_id: str | None = None
    last_polled_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_message: str | None = None
    consecutive_errors: int = Field(default=0, ge=0)


class SocialXState(BaseModel):
    """Top-level X-poller state. One file at ``state/social_x.json``."""

    model_config = ConfigDict(extra="forbid")

    handles: dict[str, HandleState] = Field(default_factory=dict)
    last_polled_at: datetime | None = None

    def for_handle(self, handle: str) -> HandleState:
        norm = handle.lstrip("@").lower()
        existing = self.handles.get(norm)
        if existing is not None:
            return existing
        fresh = HandleState(handle=norm)
        self.handles[norm] = fresh
        return fresh


def _state_path(state_dir: Path) -> Path:
    return state_dir / "social_x.json"


def load_social_x_state(state_dir: Path) -> SocialXState:
    """Read the X-poller state file, returning an empty one on miss/corrupt."""
    path = _state_path(state_dir)
    if not path.exists():
        return SocialXState()
    try:
        return SocialXState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt state must not wedge polling
        logger.exception("failed to load social_x state at %s; resetting", path)
        return SocialXState()


def save_social_x_state(state_dir: Path, state: SocialXState) -> Path:
    """Atomically write state. tmp + rename, like ``state.save_target_state``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    final = _state_path(state_dir)
    tmp = final.with_suffix(final.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(final)
    return final


# -----------------------------------------------------------------------------
# Match model
# -----------------------------------------------------------------------------


@dataclass
class XSignalMatch:
    """One tweet that matched at least one configured keyword."""

    handle: str
    user_id: str
    tweet_id: str
    text: str
    created_at: str | None
    matched_keywords: list[str]
    target_name: str | None
    label: str | None
    url: str = field(init=False)

    def __post_init__(self) -> None:
        # X's canonical tweet permalink. ``twitter.com`` 301-redirects
        # to ``x.com`` but we use the modern host directly.
        self.url = f"https://x.com/{self.handle}/status/{self.tweet_id}"


# -----------------------------------------------------------------------------
# HTTP client (thin wrapper, swappable for tests)
# -----------------------------------------------------------------------------


class HttpClient(Protocol):
    """Subset of httpx.Client we actually use. Tests inject a fake."""

    def get(  # noqa: D401 — Protocol stub
        self, url: str, *, params: dict[str, Any] | None = ..., headers: dict[str, str] | None = ...
    ) -> httpx.Response: ...


class XClient:
    """Minimal X API v2 read client. Bearer auth only, raises on HTTP error."""

    def __init__(
        self,
        bearer_token: str,
        *,
        http: HttpClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not bearer_token:
            raise ValueError("X bearer token is required (set X_BEARER_TOKEN)")
        self._bearer = bearer_token
        self._http: HttpClient = (
            http if http is not None else httpx.Client(timeout=timeout)
        )

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer}"}

    def get_user_id(self, handle: str) -> str:
        """Resolve ``@handle`` -> numeric user id. One call per handle, ever."""
        clean = handle.lstrip("@")
        resp = self._http.get(
            f"{X_API_BASE}/users/by/username/{clean}",
            headers=self._auth_headers,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        user_id = data.get("id")
        if not user_id:
            raise XApiError(
                f"users/by/username/{clean} returned no id: {payload!r}"
            )
        return str(user_id)

    def get_recent_tweets(
        self,
        user_id: str,
        *,
        since_id: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch up to ``max_results`` recent tweets, newest first.

        ``since_id`` makes the query incremental — X returns only tweets
        with id strictly greater than ``since_id``, so on a steady-state
        poll with no new content we get back an empty list.
        """
        params: dict[str, Any] = {
            # 5 is the v2-required minimum; some accounts won't have 5 new
            # tweets in a 15-min window, that's fine.
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "created_at,text,author_id,id",
            "exclude": "retweets,replies",
        }
        if since_id:
            params["since_id"] = since_id
        resp = self._http.get(
            f"{X_API_BASE}/users/{user_id}/tweets",
            params=params,
            headers=self._auth_headers,
        )
        resp.raise_for_status()
        payload = resp.json()
        return list(payload.get("data") or [])


class XApiError(RuntimeError):
    """Raised on a structurally-bad X API response."""


# -----------------------------------------------------------------------------
# Pure matcher
# -----------------------------------------------------------------------------


def match_tweet(text: str, keywords: Iterable[str]) -> list[str]:
    """Return the keywords (in original casing) that appear in ``text``.

    Case-insensitive substring match. Order-preserving, deduplicated. Empty
    keyword list -> no matches (we never want to alert on every tweet).
    """
    if not text:
        return []
    haystack = text.lower()
    seen: set[str] = set()
    hits: list[str] = []
    for kw in keywords:
        norm = kw.strip().lower()
        if not norm or norm in seen:
            continue
        if norm in haystack:
            hits.append(kw)
            seen.add(norm)
    return hits


def _effective_keywords(
    handle_cfg: SocialXHandleConfig, defaults: list[str]
) -> list[str]:
    return handle_cfg.keywords if handle_cfg.keywords else list(defaults)


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


@dataclass
class PollResult:
    """Outcome of one ``check_x_signals`` invocation."""

    matches: list[XSignalMatch] = field(default_factory=list)
    handles_polled: int = 0
    handles_failed: int = 0
    errors: list[str] = field(default_factory=list)


def check_x_signals(
    cfg: SocialXConfig,
    bearer_token: str,
    state_dir: Path,
    *,
    client: XClient | None = None,
    now: datetime | None = None,
) -> PollResult:
    """Poll every configured handle once, persist state, return matches.

    Per-handle errors are caught + logged: a single broken handle never
    aborts the rest of the sweep. The caller (loop) catches anything that
    escapes here and logs it without crashing the Fandango tick.
    """
    if not cfg.enabled:
        logger.debug("social_x disabled; skipping poll")
        return PollResult()
    if not cfg.handles:
        logger.warning("social_x enabled but no handles configured")
        return PollResult()

    effective_now = now if now is not None else datetime.now(UTC)
    state = load_social_x_state(state_dir)
    state.last_polled_at = effective_now

    x = client if client is not None else XClient(bearer_token)
    result = PollResult()

    # Dedupe by normalized handle so a handle shared across multiple movies
    # (e.g. @IMAX referenced by both Odyssey and Dune Part Three) hits the
    # X API exactly once per poll. Each tweet is then evaluated against
    # every entry in the group, so we still emit one match per movie that
    # the tweet relates to.
    groups: dict[str, list[SocialXHandleConfig]] = {}
    display_names: dict[str, str] = {}
    for h in cfg.handles:
        norm = h.handle.lstrip("@").lower()
        groups.setdefault(norm, []).append(h)
        display_names.setdefault(norm, h.handle.lstrip("@"))

    for norm_handle, entries in groups.items():
        result.handles_polled += 1
        handle_state = state.for_handle(norm_handle)
        try:
            if handle_state.user_id is None:
                handle_state.user_id = x.get_user_id(norm_handle)
                logger.info(
                    "resolved x handle @%s -> id=%s",
                    norm_handle,
                    handle_state.user_id,
                )

            tweets = x.get_recent_tweets(
                handle_state.user_id,
                since_id=handle_state.last_seen_tweet_id,
                max_results=cfg.max_results_per_handle,
            )

            new_max_id = handle_state.last_seen_tweet_id
            for tweet in tweets:
                tid = str(tweet.get("id") or "")
                if not tid:
                    continue
                if new_max_id is None or _id_gt(tid, new_max_id):
                    new_max_id = tid
                text = str(tweet.get("text") or "")
                # Per-movie evaluation: a tweet may legitimately match
                # several movies' keyword sets; emit one match per hit.
                emitted_for_this_tweet: set[tuple[str | None, str | None]] = set()
                for entry in entries:
                    keywords = _effective_keywords(entry, cfg.default_keywords)
                    hits = match_tweet(text, keywords)
                    if not hits:
                        continue
                    # Avoid an exact duplicate when two movie entries share
                    # the same (target, label) pair.
                    dedupe_key = (entry.target_name, entry.label)
                    if dedupe_key in emitted_for_this_tweet:
                        continue
                    emitted_for_this_tweet.add(dedupe_key)
                    result.matches.append(
                        XSignalMatch(
                            handle=display_names[norm_handle],
                            user_id=handle_state.user_id,
                            tweet_id=tid,
                            text=text,
                            created_at=tweet.get("created_at"),
                            matched_keywords=hits,
                            target_name=entry.target_name,
                            label=entry.label,
                        )
                    )

            handle_state.last_seen_tweet_id = new_max_id
            handle_state.last_polled_at = effective_now
            handle_state.consecutive_errors = 0
            handle_state.last_error_message = None
        except Exception as e:  # noqa: BLE001 — per-handle isolation
            result.handles_failed += 1
            result.errors.append(f"@{norm_handle}: {type(e).__name__}: {e}")
            handle_state.last_error_at = effective_now
            handle_state.last_error_message = f"{type(e).__name__}: {e}"
            handle_state.consecutive_errors += 1
            logger.exception("social_x poll failed for @%s", norm_handle)

    save_social_x_state(state_dir, state)
    return result


def _id_gt(a: str, b: str) -> bool:
    """Compare X tweet ids as integers (they're snowflake ints in string form).

    Falls back to string compare if either side isn't a clean int — should
    never happen with real API data but keeps us from crashing on a fixture
    typo.
    """
    try:
        return int(a) > int(b)
    except ValueError:
        return a > b


# -----------------------------------------------------------------------------
# Convenience for CLI / debug printing
# -----------------------------------------------------------------------------


def matches_to_jsonable(matches: list[XSignalMatch]) -> list[dict[str, Any]]:
    return [
        {
            "handle": m.handle,
            "user_id": m.user_id,
            "tweet_id": m.tweet_id,
            "url": m.url,
            "created_at": m.created_at,
            "matched_keywords": m.matched_keywords,
            "target_name": m.target_name,
            "label": m.label,
            "text": m.text,
        }
        for m in matches
    ]


__all__ = [
    "HandleState",
    "PollResult",
    "SocialXState",
    "XApiError",
    "XClient",
    "XSignalMatch",
    "check_x_signals",
    "load_social_x_state",
    "match_tweet",
    "matches_to_jsonable",
    "save_social_x_state",
]

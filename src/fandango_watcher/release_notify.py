"""Persistent dedupe for release-related SMS (survives across ticks and targets)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .movie_watch_state import load_movie_watch_state, save_movie_watch_state
from .models import ReleaseSchema
from .state import Event

if TYPE_CHECKING:
    from .config import WatcherConfig
    from .state import TargetState

logger = logging.getLogger(__name__)

_PERSISTENT_RELEASE_EVENTS = frozenset({
    Event.RELEASE_TRANSITION_BAD_TO_GOOD,
    Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED,
})


def _schema_value(schema: ReleaseSchema | str | None) -> str | None:
    if schema is None:
        return None
    return schema.value if isinstance(schema, ReleaseSchema) else str(schema)


def should_send_persistent_release_notify(
    state_dir: Path | None,
    cfg: WatcherConfig,
    *,
    target_name: str,
    event: str,
) -> bool:
    """Return False when this movie already received this release SMS."""
    if state_dir is None or event not in _PERSISTENT_RELEASE_EVENTS:
        return True
    movie = cfg.movie_for_target(target_name)
    if movie is None:
        return True
    mws = load_movie_watch_state(state_dir, movie.key)
    if event == Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED:
        return mws.disclosed_notified_at is None
    if event == Event.RELEASE_TRANSITION_BAD_TO_GOOD:
        return mws.live_notified_at is None
    return True


def mark_persistent_release_notify(
    state_dir: Path | None,
    cfg: WatcherConfig,
    *,
    target_name: str,
    event: str,
) -> None:
    if state_dir is None or event not in _PERSISTENT_RELEASE_EVENTS:
        return
    movie = cfg.movie_for_target(target_name)
    if movie is None:
        return
    now = datetime.now(UTC)
    mws = load_movie_watch_state(state_dir, movie.key)
    if event == Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED:
        mws = mws.model_copy(update={"disclosed_notified_at": now})
    elif event == Event.RELEASE_TRANSITION_BAD_TO_GOOD:
        mws = mws.model_copy(
            update={
                "live_notified_at": now,
                # Next sales window can disclose again after tickets sell through.
                "disclosed_notified_at": None,
            }
        )
    save_movie_watch_state(state_dir, mws)
    logger.info(
        "marked persistent release notify event=%s movie=%s",
        event,
        movie.key,
    )


def reset_movie_release_notify_flags(
    state_dir: Path | None,
    cfg: WatcherConfig,
    *,
    movie_key: str,
    target_states: dict[str, TargetState],
) -> None:
    """Clear notify flags when every target for the movie is back to not on sale."""
    if state_dir is None:
        return
    movie = next((m for m in cfg.movies if m.key == movie_key), None)
    if movie is None or not movie.fandango_targets:
        return

    schemas: list[str] = []
    for tname in movie.fandango_targets:
        st = target_states.get(str(tname))
        if st is None:
            continue
        val = _schema_value(st.last_release_schema)
        if val is not None:
            schemas.append(val)

    if not schemas:
        return
    if not all(s == ReleaseSchema.NOT_ON_SALE.value for s in schemas):
        return

    mws = load_movie_watch_state(state_dir, movie_key)
    if mws.disclosed_notified_at is None and mws.live_notified_at is None:
        return
    mws = mws.model_copy(
        update={"disclosed_notified_at": None, "live_notified_at": None}
    )
    save_movie_watch_state(state_dir, mws)
    logger.info("reset release notify flags for movie=%s (all targets not_on_sale)", movie_key)


def append_notification_log(
    state_dir: Path | None,
    *,
    event: str,
    target_name: str,
    movie_key: str | None,
    channels_ok: list[str],
) -> None:
    if state_dir is None or not channels_ok:
        return
    path = state_dir / "notifications.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now(UTC).isoformat(),
        "event": event,
        "target": target_name,
        "movie_key": movie_key,
        "channels": channels_ok,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

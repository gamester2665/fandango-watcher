"""Per-movie watch preferences: schedule SMS dedupe + pinned showtimes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import FormatTag

logger = logging.getLogger(__name__)


class PinnedShowtime(BaseModel):
    """A CityWalk showtime the operator wants alerts for."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    date: str = Field(min_length=1)
    time_label: str = Field(min_length=1)
    format: FormatTag | str = FormatTag.IMAX_70MM
    showtime_hash: str | None = None
    ticket_url: str | None = None
    label: str | None = None

    def pin_id(self) -> str:
        if self.showtime_hash:
            return f"hash:{self.showtime_hash}"
        fmt = self.format.value if isinstance(self.format, FormatTag) else str(self.format)
        return f"{self.date}|{self.time_label}|{fmt}"


class MovieWatchState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movie_key: str
    schedule_notified_at: datetime | None = None
    schedule_fingerprint: str | None = None
    # One SMS per movie per sales window (see release_notify.py).
    disclosed_notified_at: datetime | None = None
    live_notified_at: datetime | None = None
    pinned_showtimes: list[PinnedShowtime] = Field(default_factory=list)
    # pin_id -> last notified status (listed | live | gone)
    pin_notify_status: dict[str, str] = Field(default_factory=dict)


def _movie_state_path(state_dir: Path, movie_key: str) -> Path:
    safe = movie_key.replace("/", "_").replace("\\", "_")
    return state_dir / "movies" / f"{safe}.json"


def load_movie_watch_state(state_dir: Path, movie_key: str) -> MovieWatchState:
    path = _movie_state_path(state_dir, movie_key)
    if not path.exists():
        return MovieWatchState(movie_key=movie_key)
    try:
        data = MovieWatchState.model_validate_json(path.read_text(encoding="utf-8"))
        if data.movie_key != movie_key:
            data = data.model_copy(update={"movie_key": movie_key})
        return data
    except Exception:  # noqa: BLE001
        logger.exception("failed to load movie watch state %s; resetting", movie_key)
        return MovieWatchState(movie_key=movie_key)


def save_movie_watch_state(state_dir: Path, state: MovieWatchState) -> Path:
    path = _movie_state_path(state_dir, state.movie_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path

"""Per-target state machine + on-disk persistence.

``transition`` and ``record_error`` are pure functions that turn a previous
``TargetState`` plus the latest evidence into a new ``TargetState`` and a list
of event names that the watch loop should emit to the notifier.

State files live at ``<state_dir>/<target-name>.json`` and are written
atomically (tmp + rename) so a crash mid-write can never corrupt them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .models import ParsedPageData, ReleaseSchema

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# States + events
# -----------------------------------------------------------------------------


class WatcherState(StrEnum):
    IDLE = "idle"
    WATCHING = "watching"
    ALERTED = "alerted"
    PURCHASING = "purchasing"  # Phase 4
    AWAITING_CONFIRM = "awaiting_confirm"  # Phase 4
    PURCHASED = "purchased"  # Phase 4
    HALTED_FOR_HUMAN = "halted_for_human"  # Phase 4+


# Canonical event names. Kept as module constants so the loop, tests, and
# ``notify.on_events`` YAML config can all reference the exact same string.
class Event:
    RELEASE_TRANSITION_BAD_TO_GOOD: ClassVar[str] = "release_transition_bad_to_good"
    WATCHER_STUCK_ON_ERROR_STREAK: ClassVar[str] = "watcher_stuck_on_error_streak"
    PURCHASE_SUCCEEDED: ClassVar[str] = "purchase_succeeded"
    PURCHASE_HALTED_INVARIANT: ClassVar[str] = "purchase_halted_invariant"
    PURCHASE_HALTED_PREFERRED_SOLD_OUT: ClassVar[str] = (
        "purchase_halted_preferred_sold_out"
    )
    PURCHASE_HELD_FOR_CONFIRM: ClassVar[str] = "purchase_held_for_confirm"
    PURCHASE_FAILED_SCRIPTED: ClassVar[str] = "purchase_failed_scripted"
    SOCIAL_X_MATCH: ClassVar[str] = "social_x_match"


# -----------------------------------------------------------------------------
# TargetState: one JSON file per target
# -----------------------------------------------------------------------------


class TargetState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: int = Field(default=1, ge=1)
    target_name: str
    current_state: WatcherState = WatcherState.IDLE
    last_release_schema: ReleaseSchema | None = None
    last_tick_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_message: str | None = None
    consecutive_errors: int = Field(default=0, ge=0)
    consecutive_successes: int = Field(default=0, ge=0)
    total_ticks: int = Field(default=0, ge=0)
    total_errors: int = Field(default=0, ge=0)


class TransitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: TargetState
    events: list[str] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Pure transition logic
# -----------------------------------------------------------------------------


def _is_good(schema: ReleaseSchema | str | None) -> bool:
    if schema is None:
        return False
    # use_enum_values=True stores the .value string on the model, so we have
    # to normalize both sides here.
    value = schema.value if isinstance(schema, ReleaseSchema) else schema
    return value in {
        ReleaseSchema.PARTIAL_RELEASE.value,
        ReleaseSchema.FULL_RELEASE.value,
    }


def _is_bad_or_unknown(schema: ReleaseSchema | str | None) -> bool:
    if schema is None:
        return True
    value = schema.value if isinstance(schema, ReleaseSchema) else schema
    return value == ReleaseSchema.NOT_ON_SALE.value


def transition(
    prev: TargetState,
    parsed: ParsedPageData,
    *,
    now: datetime | None = None,
) -> TransitionResult:
    """Apply a successful crawl to ``prev`` and return the new state + events.

    Fired events (by name, matching ``notify.on_events``):

    * ``release_transition_bad_to_good`` — previous schema was None or
      ``not_on_sale`` and current is ``partial_release``/``full_release``.
      This is the core "tickets just dropped" alert.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    new_schema = parsed.release_schema

    events: list[str] = []
    if _is_bad_or_unknown(prev.last_release_schema) and _is_good(new_schema):
        events.append(Event.RELEASE_TRANSITION_BAD_TO_GOOD)

    if _is_good(new_schema):
        new_watcher_state = WatcherState.ALERTED
    else:
        new_watcher_state = WatcherState.WATCHING

    updated = prev.model_copy(
        update={
            "current_state": new_watcher_state,
            "last_release_schema": new_schema,
            "last_tick_at": effective_now,
            "last_success_at": effective_now,
            "last_error_message": None,
            "consecutive_errors": 0,
            "consecutive_successes": prev.consecutive_successes + 1,
            "total_ticks": prev.total_ticks + 1,
        }
    )
    return TransitionResult(state=updated, events=events)


def record_error(
    prev: TargetState,
    error: BaseException,
    *,
    error_streak_threshold: int = 5,
    now: datetime | None = None,
) -> TransitionResult:
    """Record a failed crawl attempt.

    Fires ``watcher_stuck_on_error_streak`` exactly once, when the streak
    crosses the threshold. Subsequent errors past the threshold stay quiet
    so we don't spam the user mid-outage.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    new_streak = prev.consecutive_errors + 1
    events: list[str] = []
    if new_streak == error_streak_threshold:
        events.append(Event.WATCHER_STUCK_ON_ERROR_STREAK)

    updated = prev.model_copy(
        update={
            "last_tick_at": effective_now,
            "last_error_at": effective_now,
            "last_error_message": f"{type(error).__name__}: {error}",
            "consecutive_errors": new_streak,
            "consecutive_successes": 0,
            "total_ticks": prev.total_ticks + 1,
            "total_errors": prev.total_errors + 1,
        }
    )
    return TransitionResult(state=updated, events=events)


# -----------------------------------------------------------------------------
# Disk persistence
# -----------------------------------------------------------------------------


def _state_path(state_dir: Path, target_name: str) -> Path:
    safe = target_name.replace("/", "_").replace("\\", "_")
    return state_dir / f"{safe}.json"


def load_target_state(state_dir: Path, target_name: str) -> TargetState:
    """Read a target's state file, falling back to a fresh ``IDLE`` record."""
    path = _state_path(state_dir, target_name)
    if not path.exists():
        return TargetState(target_name=target_name)
    try:
        return TargetState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt state must not wedge the watcher
        logger.exception(
            "failed to load state for %s; resetting to IDLE", target_name
        )
        return TargetState(target_name=target_name)


def save_target_state(state_dir: Path, state: TargetState) -> Path:
    """Atomically persist one target's state. Returns the written path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    final = _state_path(state_dir, state.target_name)
    tmp = final.with_suffix(final.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(final)
    return final

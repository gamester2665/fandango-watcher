"""Tests for src/fandango_watcher/state.py.

Covers:

* transition() fires release_transition_bad_to_good exactly on the
  not_on_sale -> {partial, full} edge (including the first-ever crawl)
* transition() does NOT re-fire on subsequent good crawls
* transition() resets the error streak on success
* record_error() fires watcher_stuck_on_error_streak exactly once at
  threshold and stays quiet past it
* load/save round-trip preserves every field including timestamps + enums
* corrupt state files fall back to IDLE without raising
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fandango_watcher.models import (
    FullReleasePageData,
    NotOnSalePageData,
    PartialReleasePageData,
    ReleaseSchema,
)
from fandango_watcher.state import (
    Event,
    TargetState,
    WatcherState,
    load_target_state,
    record_error,
    save_target_state,
    transition,
)

NOW = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


# -----------------------------------------------------------------------------
# Parsed-page fixtures
# -----------------------------------------------------------------------------


def _not_on_sale() -> NotOnSalePageData:
    return NotOnSalePageData(
        url="https://fandango.com/x",
        page_title="X",
        theater_count=0,
        showtime_count=0,
    )


def _partial_release() -> PartialReleasePageData:
    return PartialReleasePageData(
        url="https://fandango.com/x",
        page_title="X",
        theater_count=1,
        showtime_count=2,
        citywalk_present=True,
        citywalk_showtime_count=2,
    )


def _full_release() -> FullReleasePageData:
    return FullReleasePageData(
        url="https://fandango.com/x",
        page_title="X",
        theater_count=12,
        showtime_count=60,
    )


# -----------------------------------------------------------------------------
# transition()
# -----------------------------------------------------------------------------


class TestTransitionBadToGood:
    def test_first_ever_good_crawl_fires_event(self) -> None:
        prev = TargetState(target_name="odyssey-imax-70mm")
        result = transition(prev, _partial_release(), now=NOW)
        assert Event.RELEASE_TRANSITION_BAD_TO_GOOD in result.events
        assert result.state.current_state == WatcherState.ALERTED
        assert result.state.last_release_schema == ReleaseSchema.PARTIAL_RELEASE
        assert result.state.consecutive_successes == 1
        assert result.state.consecutive_errors == 0
        assert result.state.last_success_at == NOW
        assert result.state.last_tick_at == NOW

    def test_not_on_sale_to_full_release_fires_event(self) -> None:
        prev = TargetState(
            target_name="odyssey",
            last_release_schema=ReleaseSchema.NOT_ON_SALE,
            current_state=WatcherState.WATCHING,
        )
        result = transition(prev, _full_release(), now=NOW)
        assert Event.RELEASE_TRANSITION_BAD_TO_GOOD in result.events
        assert result.state.current_state == WatcherState.ALERTED

    def test_subsequent_good_crawl_does_not_re_fire(self) -> None:
        """Once we're alerted, don't spam the user every tick."""
        prev = TargetState(
            target_name="odyssey",
            last_release_schema=ReleaseSchema.PARTIAL_RELEASE,
            current_state=WatcherState.ALERTED,
        )
        result = transition(prev, _partial_release(), now=NOW)
        assert Event.RELEASE_TRANSITION_BAD_TO_GOOD not in result.events
        assert result.state.current_state == WatcherState.ALERTED

    def test_partial_to_full_does_not_fire_bad_to_good(self) -> None:
        """Both are 'good'; upgrading coverage shouldn't re-alert (MVP)."""
        prev = TargetState(
            target_name="odyssey",
            last_release_schema=ReleaseSchema.PARTIAL_RELEASE,
            current_state=WatcherState.ALERTED,
        )
        result = transition(prev, _full_release(), now=NOW)
        assert result.events == []
        assert result.state.current_state == WatcherState.ALERTED

    def test_good_to_bad_does_not_fire(self) -> None:
        """Fandango briefly dropping the page shouldn't re-arm the alert."""
        prev = TargetState(
            target_name="odyssey",
            last_release_schema=ReleaseSchema.PARTIAL_RELEASE,
            current_state=WatcherState.ALERTED,
        )
        result = transition(prev, _not_on_sale(), now=NOW)
        assert result.events == []
        assert result.state.current_state == WatcherState.WATCHING
        assert result.state.last_release_schema == ReleaseSchema.NOT_ON_SALE

    def test_success_clears_error_streak(self) -> None:
        prev = TargetState(
            target_name="odyssey",
            consecutive_errors=3,
            last_error_message="boom",
        )
        result = transition(prev, _not_on_sale(), now=NOW)
        assert result.state.consecutive_errors == 0
        assert result.state.last_error_message is None
        assert result.state.consecutive_successes == 1

    def test_counters_increment_monotonically(self) -> None:
        prev = TargetState(
            target_name="odyssey",
            total_ticks=10,
            consecutive_successes=2,
        )
        result = transition(prev, _not_on_sale(), now=NOW)
        assert result.state.total_ticks == 11
        assert result.state.consecutive_successes == 3


# -----------------------------------------------------------------------------
# record_error()
# -----------------------------------------------------------------------------


class TestRecordError:
    def test_fires_at_threshold_exactly_once(self) -> None:
        prev = TargetState(target_name="t")
        err = RuntimeError("boom")
        for _ in range(4):
            prev = record_error(prev, err, error_streak_threshold=5).state
        # 5th consecutive error crosses the threshold.
        r = record_error(prev, err, error_streak_threshold=5)
        assert Event.WATCHER_STUCK_ON_ERROR_STREAK in r.events
        assert r.state.consecutive_errors == 5
        # 6th and beyond stays quiet.
        r2 = record_error(r.state, err, error_streak_threshold=5)
        assert Event.WATCHER_STUCK_ON_ERROR_STREAK not in r2.events
        assert r2.state.consecutive_errors == 6

    def test_last_error_message_is_recorded(self) -> None:
        prev = TargetState(target_name="t")
        r = record_error(prev, ValueError("nope"), now=NOW)
        assert r.state.last_error_message == "ValueError: nope"
        assert r.state.last_error_at == NOW
        assert r.state.last_tick_at == NOW
        assert r.state.consecutive_successes == 0

    def test_error_clears_consecutive_successes(self) -> None:
        prev = TargetState(
            target_name="t",
            consecutive_successes=4,
            last_release_schema=ReleaseSchema.PARTIAL_RELEASE,
        )
        r = record_error(prev, OSError("x"))
        assert r.state.consecutive_successes == 0
        # last_release_schema is NOT cleared — we still know tickets were live.
        assert r.state.last_release_schema == ReleaseSchema.PARTIAL_RELEASE

    def test_custom_threshold_respected(self) -> None:
        prev = TargetState(target_name="t")
        r = record_error(prev, RuntimeError(), error_streak_threshold=1)
        assert Event.WATCHER_STUCK_ON_ERROR_STREAK in r.events


# -----------------------------------------------------------------------------
# Disk round-trip
# -----------------------------------------------------------------------------


class TestPersistence:
    def test_load_missing_file_returns_fresh_state(self, tmp_path: Path) -> None:
        state = load_target_state(tmp_path, "does-not-exist")
        assert state.target_name == "does-not-exist"
        assert state.current_state == WatcherState.IDLE
        assert state.last_release_schema is None
        assert state.total_ticks == 0

    def test_round_trip_preserves_all_fields(self, tmp_path: Path) -> None:
        original = TargetState(
            target_name="odyssey",
            current_state=WatcherState.ALERTED,
            last_release_schema=ReleaseSchema.PARTIAL_RELEASE,
            last_tick_at=NOW,
            last_success_at=NOW,
            last_error_at=LATER,
            last_error_message="RuntimeError: boom",
            consecutive_errors=2,
            consecutive_successes=7,
            total_ticks=100,
            total_errors=9,
        )
        path = save_target_state(tmp_path, original)
        assert path.is_file()

        loaded = load_target_state(tmp_path, "odyssey")
        assert loaded == original

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        """No '.tmp' sibling should remain after a successful save."""
        s = TargetState(target_name="t")
        save_target_state(tmp_path, s)
        tmp_files = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert tmp_files == []

    def test_corrupt_file_falls_back_to_idle(self, tmp_path: Path) -> None:
        (tmp_path / "odyssey.json").write_text("{not json at all", encoding="utf-8")
        loaded = load_target_state(tmp_path, "odyssey")
        assert loaded == TargetState(target_name="odyssey")

    def test_target_name_with_slash_is_sanitized(self, tmp_path: Path) -> None:
        s = TargetState(target_name="group/slug")
        path = save_target_state(tmp_path, s)
        assert "/" not in path.name
        assert path.is_file()

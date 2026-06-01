"""Tests for persistent release notification dedupe."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fandango_watcher.config import MovieConfig, TargetConfig, WatcherConfig
from tests.test_loop import _minimal_cfg
from fandango_watcher.movie_watch_state import load_movie_watch_state, save_movie_watch_state
from fandango_watcher.release_notify import (
    mark_persistent_release_notify,
    reset_movie_release_notify_flags,
    should_send_persistent_release_notify,
)
from fandango_watcher.state import Event, TargetState


def _supergirl_cfg(tmp_path: Path) -> WatcherConfig:
    return _minimal_cfg(tmp_path).model_copy(
        update={
            "targets": [
                TargetConfig(
                    name="supergirl-overview", url="https://example.com/a"
                ),
                TargetConfig(
                    name="supergirl-imax-70mm", url="https://example.com/b"
                ),
            ],
            "movies": [
                MovieConfig(
                    key="supergirl",
                    title="Supergirl",
                    fandango_targets=["supergirl-overview", "supergirl-imax-70mm"],
                )
            ],
        }
    )


def test_persistent_disclosed_blocks_second_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    cfg = _supergirl_cfg(tmp_path)
    mark_persistent_release_notify(
        state_dir,
        cfg,
        target_name="supergirl-overview",
        event=Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED,
    )
    assert not should_send_persistent_release_notify(
        state_dir,
        cfg,
        target_name="supergirl-imax-70mm",
        event=Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED,
    )


def test_reset_when_all_targets_not_on_sale(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    cfg = _supergirl_cfg(tmp_path)
    mws = load_movie_watch_state(state_dir, "supergirl")
    mws = mws.model_copy(
        update={"disclosed_notified_at": datetime.now(UTC)}
    )
    save_movie_watch_state(state_dir, mws)

    target_states = {
        "supergirl-overview": TargetState(
            target_name="supergirl-overview",
            last_release_schema="not_on_sale",
        ),
        "supergirl-imax-70mm": TargetState(
            target_name="supergirl-imax-70mm",
            last_release_schema="not_on_sale",
        ),
    }
    reset_movie_release_notify_flags(
        state_dir,
        cfg,
        movie_key="supergirl",
        target_states=target_states,
    )
    mws = load_movie_watch_state(state_dir, "supergirl")
    assert mws.disclosed_notified_at is None
    assert should_send_persistent_release_notify(
        state_dir,
        cfg,
        target_name="supergirl-overview",
        event=Event.RELEASE_TRANSITION_SHOWTIMES_DISCLOSED,
    )

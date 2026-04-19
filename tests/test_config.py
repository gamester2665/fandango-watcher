"""Tests for src/fandango_watcher/config.py.

Covers:

* ``load_config`` successfully parses the shipped ``config.example.yaml``
* Unknown keys anywhere in the tree cause a ValidationError (``extra='forbid'``)
* Poll min/max ordering is enforced
* Purchase seat_priority keys are validated against FormatTag
* Settings (env vars) load with defaults when no env is present
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fandango_watcher.config import (
    PollConfig,
    PurchaseConfig,
    SeatPrefEntry,
    Settings,
    TargetConfig,
    WatcherConfig,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"


# -----------------------------------------------------------------------------
# load_config on the real config.example.yaml
# -----------------------------------------------------------------------------


class TestLoadConfigHappyPath:
    @pytest.fixture(scope="class")
    def cfg(self) -> WatcherConfig:
        return load_config(CONFIG_EXAMPLE_PATH)

    def test_loads_without_errors(self, cfg: WatcherConfig) -> None:
        assert isinstance(cfg, WatcherConfig)

    def test_at_least_one_target(self, cfg: WatcherConfig) -> None:
        assert cfg.targets
        assert cfg.targets[0].url.startswith("https://www.fandango.com/")

    def test_theater_anchor(self, cfg: WatcherConfig) -> None:
        assert "CityWalk" in cfg.theater.fandango_theater_anchor

    def test_formats_require_and_include(self, cfg: WatcherConfig) -> None:
        assert "IMAX" in cfg.formats.require
        assert "IMAX_70MM" in cfg.formats.require
        assert "DOLBY" in cfg.formats.include
        assert "LASER_RECLINER" in cfg.formats.include

    def test_poll_bounds_sane(self, cfg: WatcherConfig) -> None:
        assert cfg.poll.min_seconds <= cfg.poll.max_seconds

    def test_purchase_mode_is_full_auto(self, cfg: WatcherConfig) -> None:
        assert cfg.purchase.enabled is True
        assert cfg.purchase.mode == "full_auto"

    def test_zero_dollar_invariant(self, cfg: WatcherConfig) -> None:
        assert cfg.purchase.invariant.require_total_equals == "$0.00"
        assert cfg.purchase.invariant.require_theater_match is True
        assert cfg.purchase.invariant.require_showtime_match is True
        assert cfg.purchase.invariant.require_seat_match is True
        assert cfg.purchase.invariant.require_benefit_phrase_any

    def test_seat_priority_has_all_four_formats(self, cfg: WatcherConfig) -> None:
        assert set(cfg.purchase.seat_priority.keys()) >= {
            "IMAX",
            "IMAX_70MM",
            "DOLBY",
            "LASER_RECLINER",
        }

    def test_imax_70mm_back_row(self, cfg: WatcherConfig) -> None:
        entry = cfg.purchase.seat_priority["IMAX_70MM"]
        assert entry.auditorium == 19
        assert set(entry.seats) == {
            "N10", "N11", "N12", "N13", "N14", "N15", "N16", "N17",
        }

    def test_agent_fallback_caps(self, cfg: WatcherConfig) -> None:
        assert cfg.agent_fallback.enabled is True
        assert cfg.agent_fallback.max_steps >= 1
        assert 0 < cfg.agent_fallback.max_cost_usd <= 10.0

    def test_notify_channels(self, cfg: WatcherConfig) -> None:
        assert set(cfg.notify.channels) == {"twilio", "smtp"}

    def test_screenshots_retention(self, cfg: WatcherConfig) -> None:
        assert cfg.screenshots.max_age_days == 7

    def test_browser_defaults_align_with_compose(self, cfg: WatcherConfig) -> None:
        assert cfg.browser.headless is True
        assert cfg.browser.user_data_dir == "/app/browser-profile"


# -----------------------------------------------------------------------------
# Relative paths anchored to the config file (not cwd)
# -----------------------------------------------------------------------------


class TestLoadConfigAnchorsRelativePaths:
    def test_state_dir_resolves_next_to_yaml_file(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "project" / "watcher.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(
            """
targets:
  - name: t1
    url: https://www.fandango.com/x
    wait_until: domcontentloaded
    timeout_ms: 30000
theater:
  display_name: T
  fandango_theater_anchor: AMC
formats:
  require: [IMAX]
  include: [DOLBY]
poll:
  min_seconds: 30
  max_seconds: 60
  error_backoff_multiplier: 2
  error_backoff_cap_seconds: 100
purchase:
  enabled: false
  mode: notify_only
  invariant:
    require_total_equals: "$0.00"
    require_benefit_phrase_any: ["A-List"]
    require_theater_match: false
    require_showtime_match: false
    require_seat_match: false
  seat_priority: {}
  on_preferred_sold_out: notify_only
  max_quantity: 1
notify:
  channels: []
  on_events: []
screenshots:
  dir: art/shots
  max_age_days: 7
  per_purchase_dir: art/buy
state:
  dir: mystate
browser:
  headless: true
  user_data_dir: bprof
  locale: en-US
  timezone: America/Los_Angeles
movies: []
""",
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        assert cfg.state.dir == str((cfg_path.parent / "mystate").resolve())
        assert cfg.browser.user_data_dir == str(
            (cfg_path.parent / "bprof").resolve()
        )
        assert cfg.screenshots.dir == str((cfg_path.parent / "art" / "shots").resolve())


# -----------------------------------------------------------------------------
# Strict schema: unknown keys rejected
# -----------------------------------------------------------------------------


class TestStrictExtraForbid:
    @pytest.fixture
    def base_data(self) -> dict[str, object]:
        return yaml.safe_load(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))

    def test_unknown_top_level_key_rejected(
        self, base_data: dict[str, object]
    ) -> None:
        base_data["sneaky_key"] = "oops"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WatcherConfig.model_validate(base_data)

    def test_unknown_nested_key_rejected(
        self, base_data: dict[str, object]
    ) -> None:
        base_data["purchase"]["speed_run"] = True  # type: ignore[index]
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WatcherConfig.model_validate(base_data)


# -----------------------------------------------------------------------------
# Poll bounds
# -----------------------------------------------------------------------------


class TestPollValidation:
    def test_min_must_not_exceed_max(self) -> None:
        with pytest.raises(ValidationError, match="must be <="):
            PollConfig(min_seconds=600, max_seconds=300)

    def test_equal_min_and_max_is_fine(self) -> None:
        cfg = PollConfig(min_seconds=300, max_seconds=300)
        assert cfg.min_seconds == cfg.max_seconds

    def test_below_minimum_seconds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PollConfig(min_seconds=10, max_seconds=20)  # guards against sub-minute hammering


# -----------------------------------------------------------------------------
# Purchase.seat_priority key validation
# -----------------------------------------------------------------------------


class TestSeatPriorityKeys:
    def test_unknown_format_tag_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown FormatTag"):
            PurchaseConfig.model_validate(
                {
                    "enabled": True,
                    "mode": "full_auto",
                    "seat_priority": {
                        "IMAX_70mm": {  # wrong case — should be IMAX_70MM
                            "auditorium": 19,
                            "seats": ["N10"],
                        }
                    },
                }
            )

    def test_empty_seat_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SeatPrefEntry(auditorium=1, seats=[])


# -----------------------------------------------------------------------------
# Target optional format-filter click
# -----------------------------------------------------------------------------


class TestTargetFormatFilterClick:
    def test_defaults(self) -> None:
        t = TargetConfig(name="n", url="https://www.fandango.com/x")
        assert t.format_filter_click_selector is None
        assert t.format_filter_click_label is None
        assert t.format_filter_click_timeout_ms == 12000

    def test_selector_and_label_accepted(self) -> None:
        t = TargetConfig(
            name="n",
            url="https://www.fandango.com/x",
            format_filter_click_selector="#lazyload-format-filters li",
            format_filter_click_label="IMAX 3D",
            format_filter_click_timeout_ms=8000,
        )
        assert t.format_filter_click_selector == "#lazyload-format-filters li"
        assert t.format_filter_click_label == "IMAX 3D"
        assert t.format_filter_click_timeout_ms == 8000

    def test_blank_strings_normalized_to_none(self) -> None:
        t = TargetConfig(
            name="n",
            url="https://www.fandango.com/x",
            format_filter_click_selector="",
            format_filter_click_label="   ",
        )
        assert t.format_filter_click_selector is None
        assert t.format_filter_click_label is None


# -----------------------------------------------------------------------------
# Settings (env vars)
# -----------------------------------------------------------------------------


class TestSettings:
    def test_defaults_when_env_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Make sure no inherited env / cwd .env bleeds in.
        for key in (
            "TZ",
            "WATCHER_MODE",
            "TWILIO_ACCOUNT_SID",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)

        s = Settings()
        assert s.tz == "America/Los_Angeles"
        assert s.watcher_mode == "watch"
        assert s.twilio_account_sid == ""
        assert s.openai_api_key == ""
        assert s.openrouter_api_key == ""

    def test_reads_from_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)  # avoid loading the real .env
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake1234567890")
        monkeypatch.setenv("WATCHER_MODE", "dry-run")

        s = Settings()
        assert s.twilio_account_sid == "ACfake1234567890"
        assert s.watcher_mode == "dry-run"

    def test_extra_env_vars_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Leftover keys from the user's own shell shouldn't break construction.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SOME_UNRELATED_VAR", "xyz")
        Settings()  # must not raise

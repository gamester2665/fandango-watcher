"""Tests for config.example.yaml.

The example config is the entry point for every user of this project, so it
should (a) parse, (b) carry the architectural guarantees from PLAN.md (seat
priority per format, $0.00 invariant, full_auto default, CU fallback caps),
and (c) reference only FormatTag values that actually exist in the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fandango_watcher.models import FormatTag

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"


@pytest.fixture(scope="module")
def config() -> dict[str, object]:
    raw = CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict), "config.example.yaml must parse to a mapping"
    return loaded


class TestTopLevelShape:
    def test_file_exists(self) -> None:
        assert CONFIG_EXAMPLE_PATH.is_file()

    def test_required_top_level_keys(self, config: dict[str, object]) -> None:
        expected = {
            "targets",
            "theater",
            "formats",
            "poll",
            "signal",
            "purchase",
            "agent_fallback",
            "notify",
            "screenshots",
            "browser",
        }
        assert expected.issubset(config.keys()), (
            f"missing keys: {expected - set(config.keys())}"
        )


class TestTargets:
    def test_at_least_one_target(self, config: dict[str, object]) -> None:
        targets = config["targets"]
        assert isinstance(targets, list) and targets, "targets must be a non-empty list"

    def test_every_target_has_required_fields(self, config: dict[str, object]) -> None:
        for target in config["targets"]:  # type: ignore[union-attr]
            assert "name" in target
            assert "url" in target
            assert target["url"].startswith("https://www.fandango.com/")

    def test_primary_target_is_imax_70mm_filter(self, config: dict[str, object]) -> None:
        """The first target should be the format-filtered route per PLAN.md."""
        primary = config["targets"][0]  # type: ignore[index]
        assert "IMAX%2070MM" in primary["url"]


class TestTheater:
    def test_anchor_is_citywalk(self, config: dict[str, object]) -> None:
        theater = config["theater"]
        assert "CityWalk" in theater["display_name"]  # type: ignore[index]
        assert "CityWalk" in theater["fandango_theater_anchor"]  # type: ignore[index]


class TestFormats:
    def test_require_includes_imax_and_70mm(self, config: dict[str, object]) -> None:
        formats = config["formats"]
        assert "IMAX" in formats["require"]  # type: ignore[index]
        assert "IMAX_70MM" in formats["require"]  # type: ignore[index]

    def test_every_format_tag_is_valid_enum(self, config: dict[str, object]) -> None:
        valid = {t.value for t in FormatTag}
        for key in ("require", "include"):
            for tag in config["formats"].get(key, []):  # type: ignore[union-attr]
                assert tag in valid, (
                    f"config.formats.{key} contains unknown FormatTag: {tag}"
                )


class TestPoll:
    def test_cadence_is_roughly_five_minutes(self, config: dict[str, object]) -> None:
        poll = config["poll"]
        assert 60 <= poll["min_seconds"] <= poll["max_seconds"]  # type: ignore[index]
        # Stay within sane bounds — too aggressive risks blocks, too slow misses drops.
        assert 120 <= poll["min_seconds"] <= 600  # type: ignore[index]
        assert 120 <= poll["max_seconds"] <= 900  # type: ignore[index]

    def test_error_backoff_configured(self, config: dict[str, object]) -> None:
        poll = config["poll"]
        assert poll["error_backoff_multiplier"] >= 1  # type: ignore[index]
        assert poll["error_backoff_cap_seconds"] >= 60  # type: ignore[index]


class TestPurchaseBlock:
    def test_enabled_and_full_auto_default(self, config: dict[str, object]) -> None:
        purchase = config["purchase"]
        assert purchase["enabled"] is True  # type: ignore[index]
        assert purchase["mode"] == "full_auto"  # type: ignore[index]

    def test_invariant_requires_zero_total(self, config: dict[str, object]) -> None:
        invariant = config["purchase"]["invariant"]  # type: ignore[index]
        assert invariant["require_total_equals"] == "$0.00"
        assert invariant["require_theater_match"] is True
        assert invariant["require_showtime_match"] is True
        assert invariant["require_seat_match"] is True

    def test_invariant_has_alist_benefit_allowlist(
        self, config: dict[str, object]
    ) -> None:
        phrases = config["purchase"]["invariant"]["require_benefit_phrase_any"]  # type: ignore[index]
        assert isinstance(phrases, list) and phrases
        assert any("A-List" in p or "Stubs" in p for p in phrases), (
            "At least one phrase in require_benefit_phrase_any must reference A-List / Stubs"
        )


class TestSeatPriorityMap:
    def test_all_required_formats_present(self, config: dict[str, object]) -> None:
        priority = config["purchase"]["seat_priority"]  # type: ignore[index]
        assert set(priority.keys()) >= {"IMAX", "IMAX_70MM", "DOLBY", "LASER_RECLINER"}

    def test_every_mapped_format_is_valid_enum(
        self, config: dict[str, object]
    ) -> None:
        priority = config["purchase"]["seat_priority"]  # type: ignore[index]
        valid = {t.value for t in FormatTag}
        for fmt in priority:
            assert fmt in valid, f"seat_priority references unknown FormatTag: {fmt}"

    def test_imax_70mm_back_row_preferences(
        self, config: dict[str, object]
    ) -> None:
        entry = config["purchase"]["seat_priority"]["IMAX_70MM"]  # type: ignore[index]
        assert entry["auditorium"] == 19
        assert set(entry["seats"]) == {
            "N10", "N11", "N12", "N13", "N14", "N15", "N16", "N17",
        }

    def test_dolby_prime_seats(self, config: dict[str, object]) -> None:
        entry = config["purchase"]["seat_priority"]["DOLBY"]  # type: ignore[index]
        assert entry["auditorium"] == 1
        assert set(entry["seats"]) == {"E9", "E10", "E11", "E12"}

    def test_laser_recliner_seat(self, config: dict[str, object]) -> None:
        entry = config["purchase"]["seat_priority"]["LASER_RECLINER"]  # type: ignore[index]
        assert entry["auditorium"] == 14
        assert entry["seats"] == ["H8"]

    def test_seat_strings_are_reasonable(
        self, config: dict[str, object]
    ) -> None:
        """Seat labels should look like <letter(s)><number>, e.g. N10, E9, H8."""
        import re

        seat_re = re.compile(r"^[A-Z]{1,3}\d{1,3}$")
        priority = config["purchase"]["seat_priority"]  # type: ignore[index]
        for fmt, entry in priority.items():
            for seat in entry["seats"]:
                assert seat_re.match(seat), f"{fmt}: seat {seat!r} looks malformed"


class TestAgentFallback:
    def test_enabled_with_hard_caps(self, config: dict[str, object]) -> None:
        af = config["agent_fallback"]
        assert af["enabled"] is True  # type: ignore[index]
        assert af["max_steps"] >= 5  # type: ignore[index]
        assert 0 < af["max_cost_usd"] <= 10.0, (  # type: ignore[index]
            "max_cost_usd should be a small per-invocation ceiling"
        )

    def test_invoked_only_on_scripted_failures(
        self, config: dict[str, object]
    ) -> None:
        triggers = set(config["agent_fallback"]["invoke_only_on"])  # type: ignore[index]
        assert triggers.issubset(
            {"scripted_selector_failure", "scripted_step_timeout"}
        ), "CU fallback should never be invoked by polling, only by scripted failure"


class TestNotify:
    def test_both_channels_enabled_by_default(
        self, config: dict[str, object]
    ) -> None:
        assert set(config["notify"]["channels"]) == {"twilio", "smtp"}  # type: ignore[index]

    def test_events_include_release_transition_and_purchase_outcomes(
        self, config: dict[str, object]
    ) -> None:
        events = set(config["notify"]["on_events"])  # type: ignore[index]
        required = {
            "release_transition_bad_to_good",
            "purchase_succeeded",
            "purchase_halted_invariant",
        }
        assert required.issubset(events), f"missing events: {required - events}"


class TestScreenshots:
    def test_seven_day_retention_default(self, config: dict[str, object]) -> None:
        assert config["screenshots"]["max_age_days"] == 7  # type: ignore[index]

    def test_paths_are_container_absolute(self, config: dict[str, object]) -> None:
        ss = config["screenshots"]
        assert ss["dir"].startswith("/app/")  # type: ignore[index]
        assert ss["per_purchase_dir"].startswith("/app/")  # type: ignore[index]


class TestState:
    def test_state_dir_is_container_absolute(
        self, config: dict[str, object]
    ) -> None:
        assert "state" in config
        assert config["state"]["dir"] == "/app/state"  # type: ignore[index]


class TestBrowser:
    def test_headless_production_default(self, config: dict[str, object]) -> None:
        assert config["browser"]["headless"] is True  # type: ignore[index]

    def test_profile_path_matches_compose_volume(
        self, config: dict[str, object]
    ) -> None:
        assert config["browser"]["user_data_dir"] == "/app/browser-profile"  # type: ignore[index]

    def test_timezone_is_citywalk_local(self, config: dict[str, object]) -> None:
        assert config["browser"]["timezone"] == "America/Los_Angeles"  # type: ignore[index]

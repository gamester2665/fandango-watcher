"""Tests for src/fandango_watcher/loop.py.

Drives ``run_watch`` with a fake crawl function, a capturing notifier, and
``max_ticks`` so the orchestrator can be tested without Playwright, without
real sleeps, and without a real HTTP server.

Covers:

* normal ticks: transitions fire the configured events through the notifier
* events outside ``cfg.notify.on_events`` are dropped
* errors are routed through ``record_error`` and can fire the
  ``watcher_stuck_on_error_streak`` event at threshold
* ``max_ticks`` honored; per-target state is persisted across ticks
* ``_next_sleep_seconds`` exponential backoff + cap
* ``build_notification`` copy for both implemented event types
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from fandango_watcher.config import (
    BrowserConfig,
    NotifyConfig,
    PollConfig,
    PurchaseConfig,
    SeatPrefEntry,
    Settings,
    TargetConfig,
    TheaterConfig,
    ViewportConfig,
    WatcherConfig,
    load_config,
)
from fandango_watcher.loop import (
    ERROR_STREAK_THRESHOLD,
    _next_sleep_seconds,
    build_notification,
    build_purchase_outcome_notification,
    run_watch,
)
from fandango_watcher.models import (
    FormatSection,
    FormatTag,
    NotOnSalePageData,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
)
from fandango_watcher.purchase import PurchaseAttempt, PurchaseOutcome, PurchasePlan
from fandango_watcher.notify import (
    ChannelResult,
    FanOutNotifier,
    NotificationMessage,
    Notifier,
)
from fandango_watcher.state import Event, TargetState, load_target_state

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class _CapturingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return "capture"

    def send(self, msg: NotificationMessage) -> None:
        self.sent.append(msg)


def _fanout(capture: _CapturingNotifier) -> FanOutNotifier:
    return FanOutNotifier([capture])


def _parsed_not_on_sale() -> NotOnSalePageData:
    return NotOnSalePageData(
        url="https://fandango.com/x",
        page_title="X",
        theater_count=0,
        showtime_count=0,
    )


def _parsed_partial() -> PartialReleasePageData:
    return PartialReleasePageData(
        url="https://fandango.com/x",
        page_title="X",
        theater_count=1,
        showtime_count=2,
        citywalk_present=True,
        citywalk_showtime_count=2,
        ticket_url="https://fandango.com/ticketing/abc",
    )


def _minimal_cfg(tmp_path: Path) -> WatcherConfig:
    """A WatcherConfig tuned for tests: short poll bounds, one target."""
    return WatcherConfig(
        targets=[
            TargetConfig(
                name="odyssey",
                url="https://fandango.com/x",
            )
        ],
        theater=TheaterConfig(
            display_name="AMC Universal CityWalk",
            fandango_theater_anchor="AMC Universal CityWalk",
        ),
        formats={"require": ["IMAX", "IMAX_70MM"], "include": []},  # type: ignore[arg-type]
        poll=PollConfig(
            min_seconds=30,
            max_seconds=30,
            error_backoff_multiplier=2.0,
            error_backoff_cap_seconds=1800,
        ),
        purchase={  # type: ignore[arg-type]
            "enabled": True,
            "mode": "full_auto",
            "seat_priority": {},
        },
        notify=NotifyConfig(
            channels=["twilio"],
            on_events=[
                Event.RELEASE_TRANSITION_BAD_TO_GOOD,
                Event.WATCHER_STUCK_ON_ERROR_STREAK,
            ],
        ),
        browser=BrowserConfig(
            headless=True,
            user_data_dir=str(tmp_path / "profile"),
            viewport=ViewportConfig(),
        ),
    )


def _settings() -> Settings:
    return Settings(
        tz="America/Los_Angeles",
        watcher_mode="watch",
        watcher_config="config.yaml",
        twilio_account_sid="",
        twilio_auth_token="",
        twilio_from="",
        notify_to_e164="",
        smtp_host="",
        smtp_port=465,
        smtp_user="",
        smtp_password="",
        smtp_from="",
        notify_to_email="",
        openai_api_key="",
        openrouter_api_key="",
    )


# -----------------------------------------------------------------------------
# run_watch happy path
# -----------------------------------------------------------------------------


class TestRunWatchHappyPath:
    def test_bad_to_good_transition_fires_notification(
        self, tmp_path: Path
    ) -> None:
        cfg = _minimal_cfg(tmp_path)
        settings = _settings()
        state_dir = tmp_path / "state"

        schemas = [_parsed_not_on_sale(), _parsed_partial()]

        def fake_crawl(target, **kwargs: Any):  # type: ignore[no-untyped-def]
            return schemas.pop(0)

        capture = _CapturingNotifier()

        rc = run_watch(
            cfg,
            settings,
            state_dir=state_dir,
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=2,
        )
        assert rc == 0

        events = [m.event for m in capture.sent]
        assert events == [Event.RELEASE_TRANSITION_BAD_TO_GOOD]

        # Verify the notification body contains key fields.
        msg = capture.sent[0]
        assert "odyssey" in msg.subject
        assert "partial_release" in msg.body
        assert "https://fandango.com/ticketing/abc" in msg.body

        persisted = load_target_state(state_dir, "odyssey")
        assert persisted.last_release_schema == ReleaseSchema.PARTIAL_RELEASE
        assert persisted.consecutive_successes == 2
        assert persisted.total_ticks == 2

    def test_events_not_in_on_events_are_dropped(self, tmp_path: Path) -> None:
        cfg = _minimal_cfg(tmp_path)
        cfg.notify.on_events = []  # explicit empty allowlist
        settings = _settings()

        def fake_crawl(target, **kwargs: Any):  # type: ignore[no-untyped-def]
            return _parsed_partial()

        capture = _CapturingNotifier()
        run_watch(
            cfg,
            settings,
            state_dir=tmp_path / "state",
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=1,
        )
        assert capture.sent == []

    def test_state_persists_across_runs(self, tmp_path: Path) -> None:
        """A second run_watch invocation must NOT re-fire bad_to_good."""
        cfg = _minimal_cfg(tmp_path)
        state_dir = tmp_path / "state"
        settings = _settings()

        def always_partial(target, **kwargs: Any):  # type: ignore[no-untyped-def]
            return _parsed_partial()

        cap1 = _CapturingNotifier()
        run_watch(
            cfg,
            settings,
            state_dir=state_dir,
            screenshot_dir=None,
            crawl_fn=always_partial,
            notifier=_fanout(cap1),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=1,
        )
        assert len(cap1.sent) == 1  # first tick fires the alert

        # Second run: state on disk already has last_release_schema=partial.
        cap2 = _CapturingNotifier()
        run_watch(
            cfg,
            settings,
            state_dir=state_dir,
            screenshot_dir=None,
            crawl_fn=always_partial,
            notifier=_fanout(cap2),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=1,
        )
        assert cap2.sent == []


# -----------------------------------------------------------------------------
# run_watch error paths
# -----------------------------------------------------------------------------


class TestRunWatchErrorHandling:
    def test_error_streak_fires_watcher_stuck_exactly_once(
        self, tmp_path: Path
    ) -> None:
        cfg = _minimal_cfg(tmp_path)
        settings = _settings()

        def always_raise(target, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        capture = _CapturingNotifier()
        run_watch(
            cfg,
            settings,
            state_dir=tmp_path / "state",
            screenshot_dir=None,
            crawl_fn=always_raise,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=ERROR_STREAK_THRESHOLD + 3,
        )

        events = [m.event for m in capture.sent]
        # Exactly one stuck-on-error-streak event despite the ongoing failure.
        assert events.count(Event.WATCHER_STUCK_ON_ERROR_STREAK) == 1

    def test_error_then_success_recovers_without_extra_events(
        self, tmp_path: Path
    ) -> None:
        cfg = _minimal_cfg(tmp_path)
        settings = _settings()
        state_dir = tmp_path / "state"

        schedule: list[object] = [
            RuntimeError("x"),
            _parsed_not_on_sale(),
            _parsed_partial(),
        ]

        def fake_crawl(target, **kwargs: Any):  # type: ignore[no-untyped-def]
            next_item = schedule.pop(0)
            if isinstance(next_item, BaseException):
                raise next_item
            return next_item

        capture = _CapturingNotifier()
        run_watch(
            cfg,
            settings,
            state_dir=state_dir,
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=3,
        )

        events = [m.event for m in capture.sent]
        # Streak was 1 before the recovery tick; below threshold -> no stuck event.
        assert Event.WATCHER_STUCK_ON_ERROR_STREAK not in events
        # Bad -> good transition fires once on tick 3.
        assert events == [Event.RELEASE_TRANSITION_BAD_TO_GOOD]

        persisted = load_target_state(state_dir, "odyssey")
        assert persisted.consecutive_errors == 0
        assert persisted.last_release_schema == ReleaseSchema.PARTIAL_RELEASE


# -----------------------------------------------------------------------------
# Sleep / backoff
# -----------------------------------------------------------------------------


class TestNextSleepSeconds:
    @pytest.fixture
    def rng(self) -> random.Random:
        # Fixed seed so the assertions below are deterministic.
        return random.Random(42)

    def test_no_errors_returns_within_jitter_bounds(
        self, rng: random.Random
    ) -> None:
        for _ in range(20):
            s = _next_sleep_seconds(
                min_seconds=270,
                max_seconds=330,
                backoff_multiplier=2.0,
                cap_seconds=1800,
                consecutive_errors=0,
                rng=rng,
            )
            assert 270 <= s <= 330

    def test_errors_multiply_up_to_cap(self, rng: random.Random) -> None:
        s1 = _next_sleep_seconds(
            min_seconds=300,
            max_seconds=300,  # remove jitter
            backoff_multiplier=2.0,
            cap_seconds=1800,
            consecutive_errors=1,
            rng=rng,
        )
        assert s1 == pytest.approx(300.0)  # 2^0 * 300

        s3 = _next_sleep_seconds(
            min_seconds=300,
            max_seconds=300,
            backoff_multiplier=2.0,
            cap_seconds=1800,
            consecutive_errors=3,
            rng=rng,
        )
        assert s3 == pytest.approx(1200.0)  # 2^2 * 300

        s_capped = _next_sleep_seconds(
            min_seconds=300,
            max_seconds=300,
            backoff_multiplier=2.0,
            cap_seconds=1800,
            consecutive_errors=10,
            rng=rng,
        )
        assert s_capped == 1800.0


# -----------------------------------------------------------------------------
# build_notification copy
# -----------------------------------------------------------------------------


class TestBuildNotification:
    def test_release_transition_body_has_key_fields(self) -> None:
        parsed = _parsed_partial()
        msg = build_notification(
            Event.RELEASE_TRANSITION_BAD_TO_GOOD,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            parsed=parsed,
        )
        assert msg.event == Event.RELEASE_TRANSITION_BAD_TO_GOOD
        assert "odyssey" in msg.subject
        assert "partial_release" in msg.body
        assert "https://fandango.com/ticketing/abc" in msg.body
        assert "CityWalk" in msg.body or "citywalk" in msg.body.lower()

    def test_release_attaches_screenshot_when_notify_flag_on(
        self, tmp_path: Path
    ) -> None:
        png = tmp_path / "crawl.png"
        png.write_bytes(b"x" * 300)
        parsed = _parsed_partial().model_copy(update={"screenshot_path": str(png)})
        notify = NotifyConfig(
            channels=["smtp"],
            on_events=[],
            attach_screenshots_to_email=True,
        )
        msg = build_notification(
            Event.RELEASE_TRANSITION_BAD_TO_GOOD,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            parsed=parsed,
            notify=notify,
        )
        assert len(msg.email_attachments) == 1
        assert msg.email_attachments[0][1] == png

    def test_release_transition_appends_plan_when_purchase_cfg_supplied(
        self,
    ) -> None:
        """When a full parsed page + purchase_cfg yield a real plan, the
        notification body must include the buy URL, seats, and format so
        an SMS is actionable without opening the app.
        """
        from fandango_watcher.config import (
            InvariantConfig,
            PurchaseConfig,
            SeatPrefEntry,
        )
        from fandango_watcher.models import (
            FormatSection,
            FormatTag,
            Showtime,
            TheaterListing,
        )

        parsed = PartialReleasePageData(
            url="https://fandango.com/odyssey",
            page_title="The Odyssey",
            theater_count=1,
            showtime_count=1,
            formats_seen=[FormatTag.IMAX_70MM],
            citywalk_present=True,
            citywalk_showtime_count=1,
            citywalk_formats_seen=[FormatTag.IMAX_70MM],
            theaters=[
                TheaterListing(
                    name="AMC Universal CityWalk 19",
                    is_citywalk=True,
                    format_sections=[
                        FormatSection(
                            label="IMAX 70MM",
                            normalized_format=FormatTag.IMAX_70MM,
                            showtimes=[
                                Showtime(
                                    label="7:00p",
                                    ticket_url="https://fandango.com/buy/xyz",
                                    is_buyable=True,
                                    is_citywalk=True,
                                )
                            ],
                        )
                    ],
                )
            ],
        )
        purchase_cfg = PurchaseConfig(
            enabled=True,
            mode="full_auto",
            invariant=InvariantConfig(),
            seat_priority={
                "IMAX_70MM": SeatPrefEntry(
                    auditorium=19, seats=["N10", "N11", "N12"]
                ),
            },
        )
        msg = build_notification(
            Event.RELEASE_TRANSITION_BAD_TO_GOOD,
            target_name="odyssey",
            target_url="https://fandango.com/odyssey",
            parsed=parsed,
            purchase_cfg=purchase_cfg,
        )
        assert "Plan:" in msg.body
        assert "IMAX_70MM" in msg.body
        assert "AMC Universal CityWalk 19" in msg.body
        assert "7:00p" in msg.body
        assert "https://fandango.com/buy/xyz" in msg.body
        assert "N10" in msg.body and "N12" in msg.body
        assert "full_auto" in msg.body

    def test_release_transition_without_purchase_cfg_unchanged(self) -> None:
        parsed = _parsed_partial()
        msg = build_notification(
            Event.RELEASE_TRANSITION_BAD_TO_GOOD,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            parsed=parsed,
        )
        # Legacy body (no plan section).
        assert "Plan:" not in msg.body

    def test_release_transition_no_match_when_purchase_enabled(self) -> None:
        """A page with no CityWalk showtime should emit the no-match line."""
        from fandango_watcher.config import PurchaseConfig, SeatPrefEntry

        parsed = _parsed_partial()  # no theaters list -> planner returns None
        purchase_cfg = PurchaseConfig(
            enabled=True,
            seat_priority={
                "IMAX_70MM": SeatPrefEntry(auditorium=19, seats=["N10"])
            },
        )
        msg = build_notification(
            Event.RELEASE_TRANSITION_BAD_TO_GOOD,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            parsed=parsed,
            purchase_cfg=purchase_cfg,
        )
        assert "no CityWalk showtime matched" in msg.body

    def test_watcher_stuck_body_has_streak_and_error(self) -> None:
        msg = build_notification(
            Event.WATCHER_STUCK_ON_ERROR_STREAK,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            error=RuntimeError("network down"),
            error_streak=5,
        )
        assert "5" in msg.body
        assert "network down" in msg.body
        assert "odyssey" in msg.body


def _parsed_citywalk_imax_buyable() -> PartialReleasePageData:
    return PartialReleasePageData(
        url="https://fandango.com/odyssey",
        page_title="Odyssey",
        theater_count=1,
        showtime_count=1,
        formats_seen=[FormatTag.IMAX_70MM],
        citywalk_present=True,
        citywalk_showtime_count=1,
        citywalk_formats_seen=[FormatTag.IMAX_70MM],
        theaters=[
            TheaterListing(
                name="AMC Universal CityWalk 19",
                is_citywalk=True,
                format_sections=[
                    FormatSection(
                        label="IMAX 70MM",
                        normalized_format=FormatTag.IMAX_70MM,
                        showtimes=[
                            Showtime(
                                label="7:00p",
                                ticket_url="https://fandango.com/buy/xyz",
                                is_buyable=True,
                                is_citywalk=True,
                            )
                        ],
                    )
                ],
            )
        ],
    )


class TestBuildPurchaseOutcomeNotification:
    def test_maps_success_to_purchase_succeeded(self) -> None:
        plan = PurchasePlan(
            target_name="t",
            theater_name="AMC Universal CityWalk 19",
            showtime_label="7p",
            showtime_url="https://fandango.com/buy/z",
            format_tag=FormatTag.IMAX_70MM,
            auditorium=19,
            seat_priority=["N10"],
        )
        att = PurchaseAttempt(
            plan=plan,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            outcome=PurchaseOutcome.SUCCESS,
        )
        ev, msg = build_purchase_outcome_notification(
            att, target_name="odyssey", target_url="https://fandango.com/x"
        )
        assert ev == Event.PURCHASE_SUCCEEDED
        assert "succeeded" in msg.subject.lower()
        assert "buy/z" in msg.body

    def test_purchase_outcome_attaches_screenshots_when_enabled(
        self, tmp_path: Path
    ) -> None:
        png = tmp_path / "step01.png"
        png.write_bytes(b"z" * 400)
        plan = PurchasePlan(
            target_name="t",
            theater_name="AMC Universal CityWalk 19",
            showtime_label="7p",
            showtime_url="https://fandango.com/buy/z",
            format_tag=FormatTag.IMAX_70MM,
            auditorium=19,
            seat_priority=["N10"],
        )
        att = PurchaseAttempt(
            plan=plan,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            outcome=PurchaseOutcome.SUCCESS,
            screenshots=[str(png)],
        )
        notify = NotifyConfig(
            attach_screenshots_to_email=True,
            channels=["smtp"],
            on_events=[],
        )
        _ev, msg = build_purchase_outcome_notification(
            att,
            target_name="odyssey",
            target_url="https://fandango.com/x",
            notify=notify,
        )
        assert len(msg.email_attachments) == 1
        assert msg.email_attachments[0][1] == png


class TestRunWatchPurchaseHook:
    def test_invokes_purchase_fn_after_bad_to_good_when_plan_exists(
        self, tmp_path: Path
    ) -> None:
        cfg = _minimal_cfg(tmp_path)
        cfg = cfg.model_copy(
            update={
                "purchase": PurchaseConfig(
                    enabled=True,
                    mode="full_auto",
                    seat_priority={
                        "IMAX_70MM": SeatPrefEntry(
                            auditorium=19, seats=["N10", "N11"]
                        ),
                    },
                ),
                "notify": NotifyConfig(
                    channels=["twilio"],
                    on_events=[
                        Event.RELEASE_TRANSITION_BAD_TO_GOOD,
                        Event.PURCHASE_SUCCEEDED,
                    ],
                ),
            }
        )
        seq = [_parsed_not_on_sale(), _parsed_citywalk_imax_buyable()]

        def fake_crawl(*_a: Any, **_kw: Any) -> PartialReleasePageData | NotOnSalePageData:
            return seq.pop(0)

        calls: list[PurchasePlan] = []

        def fake_purchase(plan: PurchasePlan, **kwargs: Any) -> PurchaseAttempt:
            calls.append(plan)
            return PurchaseAttempt(
                plan=plan,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                outcome=PurchaseOutcome.SUCCESS,
            )

        capture = _CapturingNotifier()
        rc = run_watch(
            cfg,
            _settings(),
            state_dir=tmp_path / "state",
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=2,
            purchase_fn=fake_purchase,
            purchase_artifacts_dir=tmp_path / "purch",
        )
        assert rc == 0
        assert len(calls) == 1
        assert calls[0].showtime_url == "https://fandango.com/buy/xyz"
        evs = [m.event for m in capture.sent]
        assert Event.RELEASE_TRANSITION_BAD_TO_GOOD in evs
        assert Event.PURCHASE_SUCCEEDED in evs

    def test_skips_purchase_when_mode_notify_only(self, tmp_path: Path) -> None:
        cfg = _minimal_cfg(tmp_path)
        cfg = cfg.model_copy(
            update={
                "purchase": PurchaseConfig(
                    enabled=True,
                    mode="notify_only",
                    seat_priority={
                        "IMAX_70MM": SeatPrefEntry(auditorium=19, seats=["N10"]),
                    },
                ),
                "notify": NotifyConfig(
                    channels=["twilio"],
                    on_events=[
                        Event.RELEASE_TRANSITION_BAD_TO_GOOD,
                        Event.PURCHASE_SUCCEEDED,
                    ],
                ),
            }
        )
        seq = [_parsed_not_on_sale(), _parsed_citywalk_imax_buyable()]

        def fake_crawl(*_a: Any, **_kw: Any) -> PartialReleasePageData | NotOnSalePageData:
            return seq.pop(0)

        calls: list[PurchasePlan] = []

        def fake_purchase(plan: PurchasePlan, **kwargs: Any) -> PurchaseAttempt:
            calls.append(plan)
            return PurchaseAttempt(
                plan=plan,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                outcome=PurchaseOutcome.SUCCESS,
            )

        capture = _CapturingNotifier()
        run_watch(
            cfg,
            _settings(),
            state_dir=tmp_path / "state2",
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=2,
            purchase_fn=fake_purchase,
        )
        assert calls == []
        assert all(m.event != Event.PURCHASE_SUCCEEDED for m in capture.sent)

    def test_purchase_fn_exception_emits_failed_scripted_event(
        self, tmp_path: Path
    ) -> None:
        cfg = _minimal_cfg(tmp_path)
        cfg = cfg.model_copy(
            update={
                "purchase": PurchaseConfig(
                    enabled=True,
                    mode="full_auto",
                    seat_priority={
                        "IMAX_70MM": SeatPrefEntry(auditorium=19, seats=["N10"]),
                    },
                ),
                "notify": NotifyConfig(
                    channels=["twilio"],
                    on_events=[
                        Event.RELEASE_TRANSITION_BAD_TO_GOOD,
                        Event.PURCHASE_FAILED_SCRIPTED,
                    ],
                ),
            }
        )
        seq = [_parsed_not_on_sale(), _parsed_citywalk_imax_buyable()]

        def fake_crawl(*_a: Any, **_kw: Any) -> PartialReleasePageData | NotOnSalePageData:
            return seq.pop(0)

        def boom(_plan: PurchasePlan, **_kw: Any) -> PurchaseAttempt:
            raise RuntimeError("no playwright in test")

        capture = _CapturingNotifier()
        run_watch(
            cfg,
            _settings(),
            state_dir=tmp_path / "state3",
            screenshot_dir=None,
            crawl_fn=fake_crawl,
            notifier=_fanout(capture),
            healthz_port=None,
            sleep_fn=lambda _s: None,
            max_ticks=2,
            purchase_fn=boom,
        )
        evs = [m.event for m in capture.sent]
        assert Event.PURCHASE_FAILED_SCRIPTED in evs
        assert any("no playwright" in m.body for m in capture.sent)

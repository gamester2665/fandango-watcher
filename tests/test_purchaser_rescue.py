"""Stubbed tests for ``run_scripted_purchase`` + agent rescue on Complete miss."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from fandango_watcher import purchaser as purchaser_mod
from fandango_watcher.agent_fallback import (
    FallbackOutcome,
    RescueRequest,
    RescueResult,
)
from fandango_watcher.config import (
    AgentFallbackConfig,
    BrowserConfig,
    PurchaseConfig,
    Settings,
)
from fandango_watcher.models import FormatTag
from fandango_watcher.purchase import PurchaseOutcome, PurchasePlan


class _FakePage:
    url = "https://www.fandango.com/checkout/review"

    def goto(self, *_a: Any, **_k: Any) -> None:
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _js: str) -> dict[str, str]:
        return {
            "title": "Review",
            "bodyText": (
                "AMC Universal CityWalk 19\n"
                "7:00p\n"
                "Seat N10\n"
                "AMC A-List Reservation\n"
                "Order Total: $0.00\n"
            ),
        }

    def screenshot(self, **_k: Any) -> None:
        return None


class _FakeCtx:
    def new_page(self) -> _FakePage:
        return _FakePage()


@contextmanager
def _fake_browser_session() -> Any:
    yield (None, _FakeCtx(), None)


def _plan() -> PurchasePlan:
    return PurchasePlan(
        target_name="t1",
        theater_name="AMC Universal CityWalk 19",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/buy/x",
        format_tag=FormatTag.IMAX_70MM,
        auditorium=19,
        seat_priority=["N10", "N11"],
        quantity=1,
    )


class _AlwaysSucceedAgent:
    name = "stub"

    def rescue(self, page: Any, request: RescueRequest) -> RescueResult:
        assert "complete" in request.failure_reason.lower()
        return RescueResult(outcome=FallbackOutcome.SUCCEEDED, notes="stub-ok")


class TestRunScriptedPurchaseAgentRescue:
    def test_retries_complete_after_successful_rescue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(purchaser_mod, "_click_seat", lambda *a, **k: True)
        monkeypatch.setattr(
            purchaser_mod, "_advance_toward_review", lambda *_a, **_k: None
        )
        calls = {"n": 0}

        def fake_complete(_page: Any) -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        monkeypatch.setattr(
            purchaser_mod, "_click_complete_reservation", fake_complete
        )
        monkeypatch.setattr(
            purchaser_mod,
            "build_agent_fallback",
            lambda _cfg, _s: _AlwaysSucceedAgent(),
        )

        att = purchaser_mod.run_scripted_purchase(
            _plan(),
            browser_cfg=BrowserConfig(
                headless=True,
                user_data_dir="./browser-profile",
            ),
            purchase_cfg=PurchaseConfig.model_validate(
                {
                    "enabled": True,
                    "mode": "full_auto",
                    "invariant": {
                        "require_benefit_phrase_any": ["A-List"],
                        "require_seat_match": False,
                    },
                }
            ),
            browser_session=_fake_browser_session(),
            settings=Settings(),
            agent_fallback_cfg=AgentFallbackConfig(enabled=True),
        )

        assert att.outcome == PurchaseOutcome.SUCCESS
        assert att.agent_rescue_attempted is True
        assert att.agent_rescue_outcome == "succeeded"
        assert calls["n"] == 2

    def test_no_rescue_when_invoke_only_on_excludes_selector_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(purchaser_mod, "_click_seat", lambda *a, **k: True)
        monkeypatch.setattr(
            purchaser_mod, "_advance_toward_review", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            purchaser_mod, "_click_complete_reservation", lambda _p: False
        )
        rescue_called = {"v": False}

        class _NeverCalled:
            name = "noopish"

            def rescue(self, page: Any, request: RescueRequest) -> RescueResult:
                rescue_called["v"] = True
                return RescueResult(outcome=FallbackOutcome.SUCCEEDED)

        monkeypatch.setattr(
            purchaser_mod,
            "build_agent_fallback",
            lambda _cfg, _s: _NeverCalled(),
        )

        att = purchaser_mod.run_scripted_purchase(
            _plan(),
            browser_cfg=BrowserConfig(
                headless=True,
                user_data_dir="./browser-profile",
            ),
            purchase_cfg=PurchaseConfig.model_validate(
                {
                    "enabled": True,
                    "mode": "full_auto",
                    "invariant": {
                        "require_benefit_phrase_any": ["A-List"],
                        "require_seat_match": False,
                    },
                }
            ),
            browser_session=_fake_browser_session(),
            settings=Settings(),
            agent_fallback_cfg=AgentFallbackConfig(
                enabled=True,
                invoke_only_on=["scripted_step_timeout"],
            ),
        )

        assert att.outcome == PurchaseOutcome.FAILED_SCRIPTED
        assert rescue_called["v"] is False
        assert att.agent_rescue_attempted is False


class TestClassifyScriptedFailure:
    def test_complete_maps_to_selector(self) -> None:
        assert (
            purchaser_mod._classify_scripted_failure_for_agent(
                err=None, complete_button_failed=True
            )
            == "scripted_selector_failure"
        )

    def test_timeout_in_err_maps_to_step_timeout(self) -> None:
        assert (
            purchaser_mod._classify_scripted_failure_for_agent(
                err="TimeoutError: 30000ms exceeded", complete_button_failed=False
            )
            == "scripted_step_timeout"
        )


class TestShouldInvokeAgentFallback:
    def test_empty_list_uses_defaults(self) -> None:
        assert purchaser_mod._should_invoke_agent_fallback(
            [], "scripted_selector_failure"
        )
        assert not purchaser_mod._should_invoke_agent_fallback(
            [], "other_reason"
        )

    def test_explicit_list(self) -> None:
        assert purchaser_mod._should_invoke_agent_fallback(
            ["a", "b"], "b"
        )
        assert not purchaser_mod._should_invoke_agent_fallback(
            ["a"], "b"
        )

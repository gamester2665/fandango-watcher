# mypy: disable-error-code=arg-type
"""Tests for src/fandango_watcher/agent_fallback.py.

Covers:

* ``build_agent_fallback`` provider selection (browser_use / noop) and
  disabled short-circuit; unknown providers raise ``ValueError``.
* :class:`NoopFallback` always returns ``DISABLED`` without touching the page.
* :class:`BrowserUseFallback` returns a clean ``FAILED`` (with install hint)
  when the optional ``browser_use`` dep is missing — no exception escapes.
* The browser-use task prompt embeds the safety rules (never click Complete,
  never enter payment data, etc.) so an upstream prompt-injection attempt
  still has to bypass the model's instruction-following first.
* ``_result_from_browser_use`` correctly maps loose result objects onto
  ``RescueResult.outcome`` (success / budget exhausted / failed).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from fandango_watcher.agent_fallback import (
    AgentFallback,
    BrowserUseFallback,
    FallbackOutcome,
    NoopFallback,
    RescueRequest,
    _result_from_browser_use,
    build_agent_fallback,
    resolve_llm_api_key_for_agent,
)
from fandango_watcher.config import AgentFallbackConfig, Settings
from fandango_watcher.purchase import PurchasePlan

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _plan() -> PurchasePlan:
    return PurchasePlan(
        target_name="odyssey-imax-70mm",
        theater_name="AMC Universal CityWalk",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/buy",
        format_tag="IMAX_70MM",
        auditorium=19,
        seat_priority=["N12", "N13", "N14"],
        quantity=1,
    )


def _request() -> RescueRequest:
    return RescueRequest(
        plan=_plan(),
        current_url="https://www.fandango.com/seats",
        failure_reason="seat-map selector not found",
        intended_movie_title="The Odyssey (2026)",
    )


class _FakePage:
    url = "https://www.fandango.com/seats"


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------


class TestBuildAgentFallback:
    def test_disabled_returns_noop(self) -> None:
        cfg = AgentFallbackConfig(enabled=False, provider="browser_use")
        impl = build_agent_fallback(cfg, Settings())
        assert isinstance(impl, NoopFallback)

    def test_browser_use_provider(self) -> None:
        cfg = AgentFallbackConfig(enabled=True, provider="browser_use")
        impl = build_agent_fallback(cfg, Settings())
        assert isinstance(impl, BrowserUseFallback)
        assert impl.name == "browser_use"

    def test_noop_provider_explicit(self) -> None:
        cfg = AgentFallbackConfig(enabled=True, provider="noop")
        assert isinstance(build_agent_fallback(cfg, Settings()), NoopFallback)

    def test_unknown_provider_raises(self) -> None:
        # Bypass the Literal validator so we exercise the runtime check.
        cfg = AgentFallbackConfig(enabled=True, provider="browser_use")
        cfg.__dict__["provider"] = "anthropic"
        with pytest.raises(ValueError, match="unknown agent_fallback.provider"):
            build_agent_fallback(cfg, Settings())


# -----------------------------------------------------------------------------
# NoopFallback
# -----------------------------------------------------------------------------


class TestNoopFallback:
    def test_returns_disabled_without_touching_page(self) -> None:
        page = object()  # NOT a real Page; would crash if touched
        result = NoopFallback().rescue(page, _request())
        assert result.outcome == FallbackOutcome.DISABLED
        assert "disabled" in result.notes.lower()


# -----------------------------------------------------------------------------
# BrowserUseFallback — prompt + missing-dep handling
# -----------------------------------------------------------------------------


class TestBrowserUsePrompt:
    """The task prompt is the second line of defense (after Python's
    invariant). Verify the safety rules are present verbatim."""

    def _prompt(self) -> str:
        return BrowserUseFallback.build_task_prompt(_request())

    def test_includes_movie_theater_showtime(self) -> None:
        p = self._prompt()
        assert "The Odyssey (2026)" in p
        assert "AMC Universal CityWalk" in p
        assert "7:00p" in p
        assert "19" in p  # auditorium

    def test_includes_seat_priority(self) -> None:
        p = self._prompt()
        for seat in ("N12", "N13", "N14"):
            assert seat in p

    def test_forbids_completing_the_purchase(self) -> None:
        p = self._prompt()
        # Hard rule #1: agent never finalizes.
        assert "Complete Reservation" in p
        assert "DO NOT" in p or "Never" in p or "never" in p

    def test_forbids_payment_data_entry(self) -> None:
        p = self._prompt()
        assert "credit card" in p.lower()
        assert "cvv" in p.lower()

    def test_handles_human_only_steps(self) -> None:
        p = self._prompt()
        assert "CAPTCHA" in p
        assert "NEEDS_HUMAN" in p


class TestBrowserUseMissingDep:
    """When ``browser_use`` isn't installed, ``rescue`` must surface a
    clean FAILED result with an install hint — not crash the purchaser."""

    def test_missing_dep_returns_failed_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the lazy import to fail even if browser_use happens to be
        # installed in the dev env.
        for mod in ("browser_use", "langchain_openai"):
            monkeypatch.setitem(sys.modules, mod, None)

        cfg = AgentFallbackConfig(enabled=True, provider="browser_use")
        impl = BrowserUseFallback(cfg, Settings())
        result = impl.rescue(_FakePage(), _request())

        assert result.outcome == FallbackOutcome.FAILED
        assert "browser-use" in result.notes
        assert "uv sync --extra agent" in result.notes


# -----------------------------------------------------------------------------
# _result_from_browser_use mapping
# -----------------------------------------------------------------------------


class TestResultMapping:
    def test_success(self) -> None:
        bu = SimpleNamespace(
            n_steps=12, is_done=True, final_result="reached review page"
        )
        r = _result_from_browser_use(bu, _FakePage(), max_steps=40)
        assert r.outcome == FallbackOutcome.SUCCEEDED
        assert r.steps_used == 12
        assert "reached review page" in r.notes
        assert r.final_url == _FakePage.url

    def test_budget_exhausted(self) -> None:
        bu = SimpleNamespace(n_steps=40, is_done=False)
        r = _result_from_browser_use(bu, _FakePage(), max_steps=40)
        assert r.outcome == FallbackOutcome.BUDGET_EXHAUSTED

    def test_plain_failure(self) -> None:
        bu = SimpleNamespace(n_steps=5, is_done=False)
        r = _result_from_browser_use(bu, _FakePage(), max_steps=40)
        assert r.outcome == FallbackOutcome.FAILED
        assert r.steps_used == 5

    def test_unknown_shape_falls_through_to_failed(self) -> None:
        bu = SimpleNamespace()  # nothing useful
        r = _result_from_browser_use(bu, _FakePage(), max_steps=40)
        assert r.outcome == FallbackOutcome.FAILED
        assert r.steps_used == 0

    def test_cost_over_max_budget_exhausted_even_if_done(self) -> None:
        bu = SimpleNamespace(
            n_steps=5,
            is_done=True,
            final_result="ok",
            usage=SimpleNamespace(total_cost=3.5),
        )
        r = _result_from_browser_use(
            bu, _FakePage(), max_steps=40, max_cost_usd=2.0
        )
        assert r.outcome == FallbackOutcome.BUDGET_EXHAUSTED
        assert r.cost_usd == 3.5
        assert "estimated_cost_usd" in r.notes

    def test_agent_history_list_style_success(self) -> None:
        class _Hist:
            def __init__(self) -> None:
                self.history = [None] * 4
                self.usage = SimpleNamespace(total_cost=0.01)
                self._done = True
                self._ok = True

            def is_done(self) -> bool:
                return self._done

            def is_successful(self) -> bool | None:
                return self._ok if self._done else None

            def final_result(self) -> str | None:
                return "review page"

        bu = _Hist()
        r = _result_from_browser_use(
            bu, _FakePage(), max_steps=40, max_cost_usd=1.0
        )
        assert r.outcome == FallbackOutcome.SUCCEEDED
        assert r.steps_used == 4
        assert r.cost_usd == 0.01


# -----------------------------------------------------------------------------
# Protocol conformance — quick structural check
# -----------------------------------------------------------------------------


class TestProtocolConformance:
    def test_noop_is_agent_fallback(self) -> None:
        impl: AgentFallback = NoopFallback()
        assert hasattr(impl, "rescue")
        assert hasattr(impl, "name")

    def test_browser_use_is_agent_fallback(self) -> None:
        impl: AgentFallback = BrowserUseFallback(
            AgentFallbackConfig(enabled=True, provider="browser_use"),
            Settings(),
        )
        assert hasattr(impl, "rescue")
        assert impl.name == "browser_use"


# -----------------------------------------------------------------------------
# resolve_llm_api_key_for_agent — OPENROUTER_API_KEY vs OPENAI_API_KEY
# -----------------------------------------------------------------------------


class TestResolveLlmApiKey:
    def test_openrouter_host_prefers_openrouter_key(self) -> None:
        s = Settings(
            openai_api_key="sk-openai-fake",
            openrouter_api_key="sk-or-v1-fake",
        )
        key = resolve_llm_api_key_for_agent(
            s, "https://openrouter.ai/api/v1"
        )
        assert key == "sk-or-v1-fake"

    def test_openrouter_host_falls_back_to_openai_when_openrouter_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = Settings(openai_api_key="sk-openai-fake", openrouter_api_key="")
        with caplog.at_level("WARNING", logger="fandango_watcher.agent_fallback"):
            key = resolve_llm_api_key_for_agent(
                s, "https://openrouter.ai/api/v1"
            )
        assert key == "sk-openai-fake"
        assert any("OPENROUTER_API_KEY" in r.message for r in caplog.records)

    def test_non_openrouter_host_prefers_openai_key(self) -> None:
        s = Settings(
            openai_api_key="sk-openai-fake",
            openrouter_api_key="sk-or-v1-fake",
        )
        key = resolve_llm_api_key_for_agent(
            s, "https://api.together.xyz/v1"
        )
        assert key == "sk-openai-fake"

    def test_non_openrouter_falls_back_to_openrouter_when_openai_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = Settings(openai_api_key="", openrouter_api_key="sk-or-v1-fake")
        with caplog.at_level("WARNING", logger="fandango_watcher.agent_fallback"):
            key = resolve_llm_api_key_for_agent(s, None)
        assert key == "sk-or-v1-fake"
        assert any("OPENAI_API_KEY" in r.message for r in caplog.records)

    def test_empty_returns_empty_string_placeholder(self) -> None:
        s = Settings(openai_api_key="", openrouter_api_key="")
        assert resolve_llm_api_key_for_agent(s, None) == "EMPTY"
        assert (
            resolve_llm_api_key_for_agent(s, "https://openrouter.ai/api/v1")
            == "EMPTY"
        )

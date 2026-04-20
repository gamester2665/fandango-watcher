"""Phase 6 — agent fallback for the scripted purchaser.

The fallback is invoked **only** when ``run_scripted_purchase`` fails
mid-checkout (selector miss, layout drift, surprise modal). Its job is to
navigate the already-open Fandango ``Page`` back to a usable review-page
state — *not* to complete the purchase. The deterministic Python
``$0.00`` invariant in :mod:`~.purchase` always re-runs after the agent
returns ``SUCCEEDED`` and the scripted code retains exclusive ownership
of the final ``Complete Reservation`` click. **No agent ever attests the
invariant itself.**

Provider abstraction lives here so the rest of the codebase never imports
the actual model client. We currently ship one real provider
(:class:`BrowserUseFallback`, OSS / vendor-neutral) plus a no-op for
``enabled: false``. The interface is intentionally provider-agnostic so
additional OSS providers (e.g. Skyvern, OmniParser-driven agents) can be
slotted in later without touching ``purchaser.py``.

Optional install
----------------

The ``browser-use`` library is an **optional** dependency. Install with::

    uv sync --extra agent

If the dependency is missing at runtime, the provider returns a clean
``FAILED`` ``RescueResult`` with an install hint instead of crashing the
purchase pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .config import AgentFallbackConfig, Settings, plain_secret
from .purchase import PurchasePlan

logger = logging.getLogger(__name__)


def resolve_llm_api_key_for_agent(settings: Settings, base_url: str | None) -> str:
    """Pick the correct bearer for ``ChatOpenAI`` given ``base_url``.

    * ``openrouter.ai`` in the URL → ``settings.openrouter_api_key`` first,
      then ``settings.openai_api_key`` as fallback (so old single-key setups
      keep working).
    * Any other host (including ``None`` for the default OpenAI endpoint) →
      ``settings.openai_api_key`` first, then ``openrouter_api_key`` if the
      former is empty (convenience for mis-typed env only — prefer setting
      the right variable for your endpoint).
    * Neither set → ``"EMPTY"`` (some self-hosted vLLM gateways accept this).
    """
    bu = (base_url or "").strip().lower()
    or_key = plain_secret(settings.openrouter_api_key).strip()
    oa_key = plain_secret(settings.openai_api_key).strip()
    if "openrouter.ai" in bu:
        if or_key:
            return or_key
        if oa_key:
            logger.warning(
                "agent_fallback.base_url is OpenRouter but OPENROUTER_API_KEY is "
                "empty; falling back to OPENAI_API_KEY. Set OPENROUTER_API_KEY to "
                "keep keys separate."
            )
            return oa_key
        return "EMPTY"

    if oa_key:
        return oa_key
    if or_key:
        logger.warning(
            "OPENAI_API_KEY is empty but OPENROUTER_API_KEY is set; using "
            "OPENROUTER_API_KEY as bearer. For non-OpenRouter endpoints set "
            "OPENAI_API_KEY instead."
        )
        return or_key
    return "EMPTY"


_BROWSER_USE_INSTALL_HINT = (
    "browser-use not installed. Run: uv sync --extra agent  "
    "(installs browser-use + langchain-openai)."
)


# -----------------------------------------------------------------------------
# Public types
# -----------------------------------------------------------------------------


class FallbackOutcome(StrEnum):
    """Coarse outcome contract every provider must satisfy."""

    SUCCEEDED = "succeeded"
    """Agent thinks the page is back to a state where the scripted
    invariant + complete-click can run again. The caller MUST re-validate
    the invariant in Python before clicking anything."""

    FAILED = "failed"
    """Agent could not recover. Caller should halt and notify the human."""

    NEEDS_HUMAN = "needs_human"
    """Agent hit a CAPTCHA, password re-prompt, 3DS, or any state that
    requires human intervention."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """``max_steps`` or ``max_cost_usd`` was reached before completion."""

    DISABLED = "disabled"
    """``agent_fallback.enabled = false`` — no rescue was attempted."""


@dataclass
class RescueRequest:
    """Everything the fallback needs to know about the in-flight attempt."""

    plan: PurchasePlan
    current_url: str
    failure_reason: str
    intended_movie_title: str | None = None
    extra_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class RescueResult:
    outcome: FallbackOutcome
    steps_used: int = 0
    cost_usd: float = 0.0
    notes: str = ""
    final_url: str | None = None


class AgentFallback(Protocol):
    """Provider interface. Implementations MUST be safe to call from a
    sync caller (``run_scripted_purchase``); async work happens internally.

    Implementations MUST NOT click ``Complete Reservation`` /
    ``Place Order`` / equivalent — that authority belongs to the scripted
    purchaser and only after :func:`~.purchase.validate_invariant` passes
    on a freshly re-read DOM.
    """

    name: str

    def rescue(self, page: Any, request: RescueRequest) -> RescueResult: ...


# -----------------------------------------------------------------------------
# No-op provider
# -----------------------------------------------------------------------------


class NoopFallback:
    """Returned when ``agent_fallback.enabled = false`` so callers never
    have to null-check the fallback handle."""

    name = "noop"

    def rescue(self, page: Any, request: RescueRequest) -> RescueResult:
        logger.info("agent_fallback disabled; skipping rescue")
        return RescueResult(
            outcome=FallbackOutcome.DISABLED,
            notes="agent_fallback disabled in config",
        )


# -----------------------------------------------------------------------------
# browser-use provider
# -----------------------------------------------------------------------------


class BrowserUseFallback:
    """Open-source rescue provider built on
    `browser-use <https://github.com/browser-use/browser-use>`_.

    The ``browser_use.Agent`` perceive→plan→act loop is driven by any
    OpenAI-compatible chat endpoint: a self-hosted vLLM serving Qwen2.5-VL,
    OpenRouter, Together AI, Fireworks, OpenAI itself — all configured via
    :attr:`~.AgentFallbackConfig.base_url` plus
    :func:`resolve_llm_api_key_for_agent` (``OPENROUTER_API_KEY`` vs
    ``OPENAI_API_KEY`` depending on host).

    The agent reuses the **already-open Playwright page** the scripted
    purchaser was driving, so warmed Fandango / AMC Stubs cookies stay live
    across the rescue.
    """

    name = "browser_use"

    def __init__(self, cfg: AgentFallbackConfig, settings: Settings) -> None:
        self._cfg = cfg
        self._settings = settings

    # --- prompt construction ------------------------------------------------

    @staticmethod
    def build_task_prompt(request: RescueRequest) -> str:
        """The system+task prompt handed to ``browser_use.Agent``.

        Static so tests can assert on the safety rules without instantiating
        the agent (which would require the optional dep).
        """
        plan = request.plan
        seats = ", ".join(plan.seat_priority[:5]) if plan.seat_priority else "(none)"
        movie = request.intended_movie_title or plan.target_name
        return (
            "You are rescuing a stuck Fandango ticket-purchase flow.\n"
            "\n"
            "GOAL: Navigate the current page so it lands on the Fandango ORDER "
            "REVIEW page showing the seat selection, the showtime, and the "
            "order total ready to be finalized. DO NOT click 'Complete "
            "Reservation' / 'Place Order' / 'Confirm Purchase' yourself -- "
            "external Python code re-validates the order total is $0.00 and "
            "owns the final click. Your job ends at the review page.\n"
            "\n"
            "CONTEXT:\n"
            f"- Movie: {movie}\n"
            f"- Theater: {plan.theater_name}\n"
            f"- Showtime: {plan.showtime_label}\n"
            f"- Auditorium: {plan.auditorium}\n"
            f"- Preferred seats (priority order): {seats}\n"
            f"- Failure that triggered rescue: {request.failure_reason}\n"
            f"- Current URL: {request.current_url}\n"
            "\n"
            "HARD RULES:\n"
            "1. Never click any button labeled 'Complete Reservation', "
            "'Place Order', 'Confirm Purchase', or anything that finalizes "
            "the transaction.\n"
            "2. Never enter credit card numbers, CVVs, or any payment data.\n"
            "3. If you hit a CAPTCHA, password re-prompt, 3DS challenge, or "
            "any human-only step, stop immediately and report 'NEEDS_HUMAN'.\n"
            "4. If preferred seats are no longer available, stop and report "
            "'PREFERRED_SOLD_OUT' -- do not pick alternates.\n"
            "5. Always prefer the highest-priority seat from the list above. "
            "If that one is taken, try the next one in order.\n"
            "6. When the review page is reached with seats selected and the "
            "order total visible, call the 'done' action.\n"
        )

    # --- lazy agent build ---------------------------------------------------

    def _build_agent(self, page: Any, task: str, *, calculate_cost: bool = False) -> Any:
        """Lazy-import ``browser_use``. Raises ``RuntimeError`` with a clear
        install hint if the optional dependency is missing."""
        try:
            from browser_use import Agent
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError(_BROWSER_USE_INSTALL_HINT) from e

        # ``base_url=None`` -> default OpenAI endpoint. Self-hosted vLLM,
        # OpenRouter, Together, Fireworks all expose an OpenAI-compatible
        # API at a custom base URL.
        api_key = resolve_llm_api_key_for_agent(
            self._settings, self._cfg.base_url
        )
        llm = ChatOpenAI(
            model=self._cfg.model,
            base_url=self._cfg.base_url or None,
            api_key=api_key,
            temperature=0.0,
        )
        return Agent(
            task=task,
            llm=llm,
            page=page,
            calculate_cost=calculate_cost,
        )

    # --- public ------------------------------------------------------------

    def rescue(self, page: Any, request: RescueRequest) -> RescueResult:
        task = self.build_task_prompt(request)

        calculate_cost = self._cfg.max_cost_usd > 0
        try:
            agent = self._build_agent(page, task, calculate_cost=calculate_cost)
        except RuntimeError as e:
            # Missing optional dep, bad config, etc. Surface as FAILED so
            # the purchaser falls through to "halt + notify human".
            logger.error("browser-use init failed: %s", e)
            return RescueResult(
                outcome=FallbackOutcome.FAILED,
                notes=str(e),
            )

        max_budget = self._cfg.max_cost_usd

        async def _on_step_end(agent: Any) -> None:
            if max_budget <= 0:
                return
            try:
                summary = await agent.token_cost_service.get_usage_summary()
            except Exception:
                logger.debug(
                    "agent_fallback: usage summary unavailable for budget check",
                    exc_info=True,
                )
                return
            if summary.total_cost > max_budget:
                logger.info(
                    "agent_fallback: stopping rescue: estimated LLM cost %.4f USD "
                    "exceeds max_cost_usd=%.4f",
                    summary.total_cost,
                    max_budget,
                )
                agent.stop()

        run_kw: dict[str, Any] = {"max_steps": self._cfg.max_steps}
        if max_budget > 0:
            run_kw["on_step_end"] = _on_step_end

        try:
            # ``browser_use.Agent.run`` is async; we own the event loop here
            # because the scripted purchaser is sync.
            async def _run_agent() -> Any:
                return await asyncio.wait_for(
                    agent.run(**run_kw),
                    timeout=float(self._cfg.max_wall_seconds),
                )

            result = asyncio.run(_run_agent())
        except TimeoutError:
            logger.warning(
                "browser-use rescue exceeded max_wall_seconds=%s",
                self._cfg.max_wall_seconds,
            )
            return RescueResult(
                outcome=FallbackOutcome.FAILED,
                notes=(
                    f"agent rescue wall-clock timeout after {self._cfg.max_wall_seconds}s"
                ),
                final_url=getattr(page, "url", None),
            )
        except Exception as e:  # noqa: BLE001 — surface every failure
            logger.exception("browser-use rescue raised")
            return RescueResult(
                outcome=FallbackOutcome.FAILED,
                notes=f"{type(e).__name__}: {e}",
                final_url=getattr(page, "url", None),
            )

        return _result_from_browser_use(
            result,
            page,
            max_steps=self._cfg.max_steps,
            max_cost_usd=self._cfg.max_cost_usd,
        )


def _infer_steps(result: Any) -> int:
    hist = getattr(result, "history", None)
    if isinstance(hist, list):
        return len(hist)
    raw = getattr(result, "n_steps", None) or getattr(result, "steps", None)
    if isinstance(raw, int):
        return raw
    return 0


def _infer_cost_usd(result: Any) -> float:
    usage = getattr(result, "usage", None)
    if usage is None:
        return 0.0
    raw = getattr(usage, "total_cost", None)
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0


def _infer_success(result: Any) -> bool:
    """browser-use 0.12+ returns :class:`AgentHistoryList` with methods; older
    shapes used simple attributes."""
    is_done_m = getattr(result, "is_done", None)
    is_successful_m = getattr(result, "is_successful", None)
    if callable(is_done_m) and callable(is_successful_m):
        if not is_done_m():
            return False
        ok = is_successful_m()
        return ok is True
    return any(
        bool(getattr(result, a, False))
        for a in ("is_done", "is_successful", "success")
    )


def _result_from_browser_use(
    result: Any,
    page: Any,
    *,
    max_steps: int,
    max_cost_usd: float = 0.0,
) -> RescueResult:
    """Map browser-use's loose result object onto our typed contract.

    browser-use's API has churned across versions; we read defensively and
    fall back to FAILED if the shape is unrecognizable.
    """
    steps = _infer_steps(result)
    cost_usd = _infer_cost_usd(result)
    success = _infer_success(result)

    notes = ""
    final_fn = getattr(result, "final_result", None)
    if callable(final_fn):
        value = final_fn()
        if value:
            notes = str(value)[:500]
    if not notes:
        for attr in ("final_result", "extracted_content", "summary"):
            value = getattr(result, attr, None)
            if value:
                notes = str(value)[:500]
                break

    budget_note = ""
    if max_cost_usd > 0 and cost_usd > max_cost_usd:
        budget_note = (
            f"estimated_cost_usd={cost_usd:.4f} exceeds max_cost_usd={max_cost_usd:.4f}"
        )

    if max_cost_usd > 0 and cost_usd > max_cost_usd:
        outcome = FallbackOutcome.BUDGET_EXHAUSTED
        if budget_note:
            notes = f"{notes}; {budget_note}" if notes else budget_note
            notes = notes[:500]
    elif success:
        outcome = FallbackOutcome.SUCCEEDED
    elif not success and steps >= max_steps:
        outcome = FallbackOutcome.BUDGET_EXHAUSTED
    else:
        outcome = FallbackOutcome.FAILED

    return RescueResult(
        outcome=outcome,
        steps_used=steps,
        cost_usd=cost_usd,
        notes=notes,
        final_url=getattr(page, "url", None),
    )


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------


def build_agent_fallback(
    cfg: AgentFallbackConfig, settings: Settings
) -> AgentFallback:
    """Construct the configured fallback provider (or :class:`NoopFallback`
    if disabled / unknown)."""
    if not cfg.enabled:
        return NoopFallback()

    provider = cfg.provider.lower()
    if provider in ("noop", "none", "off"):
        return NoopFallback()
    if provider == "browser_use":
        return BrowserUseFallback(cfg, settings)

    raise ValueError(
        f"unknown agent_fallback.provider={cfg.provider!r}. "
        f"Valid: browser_use | noop"
    )


__all__ = [
    "AgentFallback",
    "BrowserUseFallback",
    "FallbackOutcome",
    "NoopFallback",
    "RescueRequest",
    "RescueResult",
    "build_agent_fallback",
    "resolve_llm_api_key_for_agent",
]

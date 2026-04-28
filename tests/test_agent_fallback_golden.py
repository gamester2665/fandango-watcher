# mypy: disable-error-code=arg-type
"""Golden test — the Python ``$0.00`` invariant gates the final click
even when the agent fallback claims SUCCEEDED.

The threat model: a future model regression, prompt-injection attack, or
a buggy provider implementation reports ``FallbackOutcome.SUCCEEDED`` on
a Fandango review page that actually shows ``$5.99`` upcharge. The
purchaser MUST refuse to click Complete in that case.

This test does NOT exercise the full Playwright session (see
``test_purchaser_rescue.py`` for a stubbed rescue + retry path). It locks
in the contract documented
in ``agent_fallback.py``::

    Returning SUCCEEDED means 'review page is now reachable and ready',
    NOT 'I bought the ticket'.

If anyone ever wires the fallback into ``run_scripted_purchase`` such
that a SUCCEEDED outcome bypasses ``validate_invariant`` on the post-
rescue DOM, this test will catch it via the documented contract.
"""

from __future__ import annotations

import pytest

from fandango_watcher.agent_fallback import (
    BrowserUseFallback,
    FallbackOutcome,
    NoopFallback,
    RescueRequest,
    RescueResult,
)
from fandango_watcher.config import InvariantConfig
from fandango_watcher.purchase import PurchasePlan, validate_invariant
from fandango_watcher.purchaser import extract_review_state


def _plan() -> PurchasePlan:
    return PurchasePlan(
        target_name="odyssey-imax-70mm",
        theater_name="AMC Universal CityWalk",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/buy",
        format_tag="IMAX_70MM",
        auditorium=19,
        seat_priority=["N12", "N13"],
        quantity=1,
    )


def _upcharge_review_snapshot() -> dict[str, str]:
    """A review page where the rescue agent recovered navigation but the
    final total is $5.99 (e.g. event-screening 70mm outside A-List)."""
    return {
        "title": "Order Review - Fandango",
        "bodyText": (
            "Order Review\n"
            "The Odyssey (2026)\n"
            "AMC Universal CityWalk\n"
            "Auditorium 19\n"
            "Wed, May 13 - 7:00p\n"
            "Seat N12\n"
            "Subtotal: $25.98\n"
            "Order Total: $5.99\n"
            "Complete Reservation"
        ),
    }


def _zero_review_snapshot() -> dict[str, str]:
    return {
        "title": "Order Review - Fandango",
        "bodyText": (
            "Order Review\n"
            "The Odyssey (2026)\n"
            "AMC Universal CityWalk\n"
            "Auditorium 19\n"
            "Wed, May 13 - 7:00p\n"
            "Seat N12\n"
            "AMC A-List Reservation\n"
            "Order Total: $0.00\n"
            "Complete Reservation"
        ),
    }


# -----------------------------------------------------------------------------
# Contract: SUCCEEDED + $5.99 review = invariant must HALT
# -----------------------------------------------------------------------------


class TestSucceededOutcomeNeverBypassesInvariant:
    """No matter what the agent reports, the final click must depend on a
    fresh Python re-validation of the review DOM."""

    def test_succeeded_with_upcharge_dom_halts_invariant(self) -> None:
        plan = _plan()
        agent_result = RescueResult(
            outcome=FallbackOutcome.SUCCEEDED,
            steps_used=12,
            notes="agent claims review page reached",
        )
        # The contract: SUCCEEDED is a hint, not a verdict. The caller
        # MUST run validate_invariant on a fresh DOM read.
        assert agent_result.outcome == FallbackOutcome.SUCCEEDED

        review = extract_review_state(plan, _upcharge_review_snapshot())
        result = validate_invariant(plan, review, InvariantConfig())

        assert result.ok is False, (
            "kill switch broken: agent reported SUCCEEDED on a $5.99 page "
            "and the Python invariant let it through. This is the exact "
            "scenario the project's safety model exists to prevent."
        )
        assert any(
            "total_mismatch" in r or "$5" in r or "5.99" in r.lower()
            for r in result.reasons_failed
        ), f"expected a total-mismatch failure; got {result.reasons_failed}"

    def test_succeeded_with_zero_dom_passes_invariant(self) -> None:
        """Sanity-check the inverse: when the agent is right AND the DOM
        is clean, the invariant passes. Otherwise the test above could
        be passing for the wrong reason."""
        plan = _plan()
        review = extract_review_state(plan, _zero_review_snapshot())
        result = validate_invariant(
            plan,
            review,
            InvariantConfig(require_seat_match=False),
        )
        assert result.ok, (
            f"$0.00 fixture should have passed; "
            f"failed reasons: {result.reasons_failed}"
        )


# -----------------------------------------------------------------------------
# Contract: every non-SUCCEEDED outcome short-circuits before any Complete click
# -----------------------------------------------------------------------------


class TestNonSucceededOutcomesAreNonClick:
    """If the agent didn't reach SUCCEEDED the purchaser must not even
    *consider* clicking Complete — regardless of what the DOM happens
    to show. Encoded here so any future wiring respects it."""

    @pytest.mark.parametrize(
        "outcome",
        [
            FallbackOutcome.FAILED,
            FallbackOutcome.NEEDS_HUMAN,
            FallbackOutcome.BUDGET_EXHAUSTED,
            FallbackOutcome.DISABLED,
        ],
    )
    def test_non_succeeded_outcomes_must_be_treated_as_halt(
        self, outcome: FallbackOutcome
    ) -> None:
        result = RescueResult(outcome=outcome)
        # The contract is purely declarative: any caller branching on
        # `outcome == SUCCEEDED` to decide whether to re-check the DOM
        # is correct; any caller assuming "rescue ran" => "safe to click"
        # is wrong.
        assert result.outcome != FallbackOutcome.SUCCEEDED


# -----------------------------------------------------------------------------
# Contract: NoopFallback always returns DISABLED (cannot accidentally
# return SUCCEEDED via mis-config)
# -----------------------------------------------------------------------------


class TestNoopCannotForgeSuccess:
    def test_noop_returns_disabled_with_zero_dom(self) -> None:
        result = NoopFallback().rescue(
            page=object(), request=_make_request()
        )
        assert result.outcome == FallbackOutcome.DISABLED

    def test_noop_returns_disabled_with_upcharge_dom(self) -> None:
        # The page state is irrelevant — Noop never touches it.
        result = NoopFallback().rescue(
            page=object(), request=_make_request()
        )
        assert result.outcome == FallbackOutcome.DISABLED


# -----------------------------------------------------------------------------
# Contract: BrowserUseFallback's task prompt forbids the final click
# -----------------------------------------------------------------------------


class TestBrowserUsePromptForbidsFinalClick:
    """The task prompt is the model-side complement to the Python
    invariant. If a future edit accidentally weakens the prompt to
    'go ahead and complete the purchase', this test fails."""

    def test_prompt_forbids_completing_purchase(self) -> None:
        prompt = BrowserUseFallback.build_task_prompt(_make_request())
        assert "Complete Reservation" in prompt
        assert "Place Order" in prompt
        # Negative space: the prompt must not contain anything that
        # contradicts the no-click rule.
        forbidden_phrases = [
            "complete the purchase",
            "finalize the order",
            "submit the payment",
            "buy the ticket",
        ]
        lowered = prompt.lower()
        for bad in forbidden_phrases:
            assert bad not in lowered, (
                f"prompt regression: contains {bad!r}, which contradicts "
                f"the no-final-click safety rule."
            )


def _make_request() -> RescueRequest:
    return RescueRequest(
        plan=_plan(),
        current_url="https://www.fandango.com/seats",
        failure_reason="seat-map selector not found",
        intended_movie_title="The Odyssey (2026)",
    )

"""Regression: rescue task prompt keeps safety clauses for real-shaped failure reasons.

See ``tests/fixtures/rescue/README.md`` and ``example_failure_reasons.json``.
"""

# mypy: disable-error-code=arg-type
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fandango_watcher.agent_fallback import BrowserUseFallback, RescueRequest
from fandango_watcher.purchase import PurchasePlan


def _plan() -> PurchasePlan:
    return PurchasePlan(
        target_name="odyssey-imax-70mm",
        theater_name="AMC Universal CityWalk",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/buy",
        format_tag="IMAX_70MM",
        auditorium=19,
        seat_priority=["N12"],
        quantity=1,
    )


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rescue" / "example_failure_reasons.json"


class TestRescuePromptSafetyRegression:
    @pytest.mark.parametrize(
        "reason",
        json.loads(_FIXTURE.read_text(encoding="utf-8"))["examples"],
    )
    def test_prompt_includes_hard_rules_for_each_fixture_reason(self, reason: str) -> None:
        req = RescueRequest(
            plan=_plan(),
            current_url="https://www.fandango.com/checkout/review",
            failure_reason=reason,
            intended_movie_title="The Odyssey (2026)",
        )
        prompt = BrowserUseFallback.build_task_prompt(req)
        assert "Complete Reservation" in prompt or "Complete" in prompt
        assert "NEEDS_HUMAN" in prompt
        assert "PREFERRED_SOLD_OUT" in prompt
        assert "credit card" in prompt.lower() or "payment" in prompt.lower()

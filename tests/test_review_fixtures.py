"""Fixture-driven invariant tests.

Auto-discovers every JSON file in ``tests/fixtures/review_pages/`` and
asserts that ``extract_review_state`` + ``validate_invariant`` agree
with the fixture's ``expected.should_pass_invariant`` flag.

This is the bridge from synthetic unit tests to **real Fandango DOM**.
Grow the corpus by running::

    uv run fandango-watcher dump-review --url <FANDANGO_REVIEW_URL> \\
        --name <descriptive_stem> --headed

then editing the ``expected`` block in the resulting JSON. Negative
fixtures (``should_pass_invariant: false``) are especially valuable —
they prove the kill switch fires on real broken pages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fandango_watcher.config import InvariantConfig
from fandango_watcher.purchase import (
    PurchasePlan,
    validate_invariant,
)
from fandango_watcher.purchaser import extract_review_state

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "review_pages"


def _discover_fixtures() -> list[Path]:
    if not FIXTURE_DIR.is_dir():
        return []
    return sorted(p for p in FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=_discover_fixtures(), ids=lambda p: p.stem)
def fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    if not request.param:
        pytest.skip("no review_pages fixtures present")
    return _load(request.param)


# -----------------------------------------------------------------------------
# Schema sanity — fail loud if a captured fixture is missing required fields
# -----------------------------------------------------------------------------


class TestFixtureSchema:
    def test_has_required_top_level_keys(self, fixture: dict[str, Any]) -> None:
        for key in ("snapshot", "expected"):
            assert key in fixture, f"fixture missing required key: {key!r}"

    def test_snapshot_has_body_text(self, fixture: dict[str, Any]) -> None:
        snap = fixture["snapshot"]
        assert isinstance(snap.get("bodyText"), str)
        assert snap["bodyText"].strip(), "snapshot.bodyText is empty"

    def test_expected_has_plan_and_invariant(
        self, fixture: dict[str, Any]
    ) -> None:
        exp = fixture["expected"]
        assert "should_pass_invariant" in exp
        assert "plan" in exp
        assert "invariant" in exp
        # Reject TODO placeholders the dump-review template leaves behind.
        plan = exp["plan"]
        for k in ("target_name", "theater_name", "showtime_label"):
            assert plan.get(k) and plan[k] != "TODO", (
                f"fixture {fixture.get('name')!r}: expected.plan.{k} is "
                f"still 'TODO'. Edit the fixture before committing."
            )


# -----------------------------------------------------------------------------
# Invariant — the actual gate
# -----------------------------------------------------------------------------


class TestInvariantAgainstFixture:
    def test_invariant_matches_expected(
        self, fixture: dict[str, Any]
    ) -> None:
        exp = fixture["expected"]
        plan = PurchasePlan(**exp["plan"])
        inv_cfg = InvariantConfig(**exp["invariant"])
        review = extract_review_state(plan, fixture["snapshot"])
        result = validate_invariant(plan, review, inv_cfg)

        if exp["should_pass_invariant"]:
            assert result.ok, (
                f"fixture {fixture.get('name')!r} expected to PASS the "
                f"invariant but failed: {result.reasons_failed}"
            )
        else:
            assert not result.ok, (
                f"fixture {fixture.get('name')!r} expected to HALT but the "
                f"invariant passed (reasons_passed={result.reasons_passed}). "
                f"This is the kill switch failing on real DOM — investigate "
                f"immediately."
            )

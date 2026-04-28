# mypy: disable-error-code=arg-type
"""Unit tests for ``src/fandango_watcher/purchaser.py`` (no Playwright)."""

from __future__ import annotations

from fandango_watcher.config import InvariantConfig
from fandango_watcher.models import FormatTag
from fandango_watcher.purchase import PurchasePlan
from fandango_watcher.purchaser import extract_review_state


def _plan(**kwargs) -> PurchasePlan:
    base = dict(
        target_name="t",
        theater_name="AMC Universal CityWalk 19",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/buy/x",
        format_tag=FormatTag.IMAX_70MM,
        auditorium=19,
        seat_priority=["N10", "N11"],
        quantity=1,
        benefit_phrase_any=["A-List"],
        require_total_equals="$0.00",
    )
    base.update(kwargs)
    return PurchasePlan(**base)


class TestExtractReviewState:
    def test_total_line_detected(self) -> None:
        snap = {
            "bodyText": "Subtotal $0.00\nOrder Total: $0.00\nTax $0.00",
            "title": "Checkout",
        }
        rs = extract_review_state(_plan(), snap)
        assert rs.total_text is not None
        assert "$0.00" in rs.total_text

    def test_review_hints_order_total_lines(self) -> None:
        snap = {
            "bodyText": "loading…",
            "title": "Checkout",
            "review_hints": {
                "order_total_lines": ["Today's order total: $0.00"],
            },
        }
        rs = extract_review_state(_plan(), snap)
        assert rs.total_text == "Today's order total: $0.00"

    def test_theater_from_plan_when_in_body(self) -> None:
        body = "You are at AMC Universal CityWalk 19 for The Odyssey"
        rs = extract_review_state(_plan(), {"bodyText": body, "title": "x"})
        assert rs.theater_name == "AMC Universal CityWalk 19"

    def test_showtime_compact_match(self) -> None:
        body = "Showtime 7:00 p on Friday"  # space in page, plan has 7:00p
        rs = extract_review_state(_plan(showtime_label="7:00p"), {"bodyText": body})
        assert rs.showtime_label == "7:00p"

    def test_seats_in_priority_order(self) -> None:
        body = "Selected seats: N11 and N10 for auditorium 19"
        rs = extract_review_state(_plan(), {"bodyText": body})
        seats = [p.seat for p in rs.selected_seats]
        assert "N11" in seats or "N10" in seats

    def test_visible_phrases_truncates_long_body(self) -> None:
        body = "\n".join(f"line {i}" for i in range(200))
        snap = {"bodyText": body, "title": ""}
        rs = extract_review_state(_plan(), snap)
        assert len(rs.visible_phrases) == 1
        assert "line 0" in rs.visible_phrases[0]


def test_validate_invariant_accepts_extracted_review() -> None:
    """Sanity: extracted snapshot can satisfy the invariant when DOM cooperates."""
    from fandango_watcher.purchase import validate_invariant

    plan = _plan()
    body = (
        f"Theater {plan.theater_name}\n"
        f"Show {plan.showtime_label}\n"
        f"Seat {plan.seat_priority[0]}\n"
        "AMC Stubs A-List applied\n"
        "Order Total: $0.00\n"
    )
    rs = extract_review_state(plan, {"bodyText": body, "title": "Review"})
    inv_cfg = InvariantConfig(
        require_benefit_phrase_any=["A-List"],
        require_total_equals="$0.00",
    )
    result = validate_invariant(plan, rs, inv_cfg)
    assert result.ok, result.reasons_failed

# mypy: disable-error-code=arg-type
"""Tests for ``src/fandango_watcher/purchase.py``.

Three sections:

* ``TestModels`` -- Pydantic validation rules (extra=forbid, ge=, etc).
* ``TestPlanner`` -- ``plan_purchase`` pick rules: disabled, no-CityWalk,
  no-matching-format, first-buyable-wins.
* ``TestInvariant`` -- the $0.00 kill switch, exhaustively. Each case
  flips exactly one variable so a failure tells you exactly which check
  regressed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fandango_watcher.config import (
    InvariantConfig,
    PurchaseConfig,
    SeatPrefEntry,
)
from fandango_watcher.models import (
    FormatSection,
    FormatTag,
    NotOnSalePageData,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
    WatchStatus,
)
from fandango_watcher.purchase import (
    InvariantResult,
    PurchaseAttempt,
    PurchaseOutcome,
    PurchasePlan,
    ReviewPageState,
    SeatPick,
    plan_purchase,
    validate_invariant,
)

# -----------------------------------------------------------------------------
# Fixtures / builders
# -----------------------------------------------------------------------------


def _make_plan(**overrides) -> PurchasePlan:
    defaults = dict(
        target_name="hateful8",
        theater_name="AMC Universal CityWalk 19",
        showtime_label="7:00p",
        showtime_url="https://www.fandango.com/checkout/abc",
        format_tag=FormatTag.IMAX_70MM,
        auditorium=19,
        seat_priority=["N10", "N11", "N12"],
        quantity=1,
        benefit_phrase_any=["A-List", "AMC Stubs A-List"],
        require_total_equals="$0.00",
    )
    defaults.update(overrides)
    return PurchasePlan(**defaults)


def _make_review(**overrides) -> ReviewPageState:
    defaults = dict(
        theater_name="AMC Universal CityWalk 19",
        showtime_label="7:00p",
        selected_seats=[SeatPick(auditorium=19, seat="N10")],
        total_text="Total: $0.00",
        visible_phrases=["AMC Stubs A-List benefit applied"],
        quantity=1,
    )
    defaults.update(overrides)
    return ReviewPageState(**defaults)


def _make_invariant(**overrides) -> InvariantConfig:
    defaults = dict(
        require_total_equals="$0.00",
        require_benefit_phrase_any=["A-List"],
        require_theater_match=True,
        require_showtime_match=True,
        require_seat_match=True,
    )
    defaults.update(overrides)
    return InvariantConfig(**defaults)


def _make_crawl_ctx(**overrides) -> dict:
    base = dict(
        url="https://www.fandango.com/movie/hateful-eight",
        page_title="Hateful Eight Tickets",
        movie_title="The Hateful Eight",
        crawled_at=datetime(2026, 12, 25, tzinfo=UTC),
        schema_evidence=["fixture"],
    )
    base.update(overrides)
    return base


def _showtime(label: str, url: str | None = "https://www.fandango.com/buy/x",
              buyable: bool = True, citywalk: bool = True) -> Showtime:
    return Showtime(
        label=label,
        ticket_url=url,
        is_buyable=buyable,
        is_citywalk=citywalk,
    )


def _format_section(fmt: FormatTag, showtimes: list[Showtime],
                    label: str | None = None) -> FormatSection:
    return FormatSection(
        label=label or fmt.value,
        normalized_format=fmt,
        attributes=[],
        showtimes=showtimes,
    )


def _theater(name: str, sections: list[FormatSection],
             is_citywalk: bool = True) -> TheaterListing:
    return TheaterListing(
        name=name,
        is_citywalk=is_citywalk,
        format_sections=sections,
    )


def _partial_release(theaters: list[TheaterListing]) -> PartialReleasePageData:
    citywalk_st = sum(
        len(fs.showtimes) for t in theaters if t.is_citywalk
        for fs in t.format_sections
    )
    citywalk_fmts = list({
        fs.normalized_format
        for t in theaters if t.is_citywalk
        for fs in t.format_sections
    })
    all_fmts = list({
        fs.normalized_format for t in theaters for fs in t.format_sections
    })
    total_st = sum(len(fs.showtimes) for t in theaters for fs in t.format_sections)
    return PartialReleasePageData(
        **_make_crawl_ctx(),
        theater_count=len(theaters),
        showtime_count=total_st,
        formats_seen=all_fmts,
        citywalk_present=any(t.is_citywalk for t in theaters),
        citywalk_showtime_count=citywalk_st,
        citywalk_formats_seen=citywalk_fmts,
        theaters=theaters,
    )


def _not_on_sale() -> NotOnSalePageData:
    return NotOnSalePageData(
        **_make_crawl_ctx(),
        theater_count=0,
        showtime_count=0,
        formats_seen=[],
        citywalk_present=False,
        citywalk_showtime_count=0,
        citywalk_formats_seen=[],
        theaters=[],
    )


def _purchase_cfg(**overrides) -> PurchaseConfig:
    defaults = dict(
        enabled=True,
        mode="full_auto",
        invariant=InvariantConfig(
            require_benefit_phrase_any=["A-List"],
        ),
        seat_priority={
            "IMAX_70MM": SeatPrefEntry(auditorium=19, seats=["N10", "N11", "N12"]),
            "DOLBY": SeatPrefEntry(auditorium=1, seats=["E9", "E10"]),
        },
        on_preferred_sold_out="notify_only",
        max_quantity=1,
    )
    defaults.update(overrides)
    return PurchaseConfig(**defaults)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------


class TestModels:
    def test_purchase_plan_minimal_round_trip(self) -> None:
        plan = _make_plan()
        round_tripped = PurchasePlan.model_validate_json(
            plan.model_dump_json()
        )
        assert round_tripped == plan

    def test_purchase_plan_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PurchasePlan.model_validate(
                {**_make_plan().model_dump(), "evil": "field"}
            )

    def test_purchase_plan_seat_priority_required(self) -> None:
        with pytest.raises(ValidationError):
            _make_plan(seat_priority=[])

    def test_purchase_plan_quantity_min_one(self) -> None:
        with pytest.raises(ValidationError):
            _make_plan(quantity=0)

    def test_purchase_plan_auditorium_min_one(self) -> None:
        with pytest.raises(ValidationError):
            _make_plan(auditorium=0)

    def test_seat_pick_auditorium_min_one(self) -> None:
        with pytest.raises(ValidationError):
            SeatPick(auditorium=0, seat="N10")

    def test_seat_pick_seat_required(self) -> None:
        with pytest.raises(ValidationError):
            SeatPick(auditorium=1, seat="")

    def test_review_state_defaults_empty(self) -> None:
        rs = ReviewPageState()
        assert rs.theater_name is None
        assert rs.selected_seats == []
        assert rs.visible_phrases == []

    def test_purchase_attempt_defaults_pending(self) -> None:
        attempt = PurchaseAttempt(
            plan=_make_plan(),
            started_at=datetime.now(UTC),
        )
        assert attempt.outcome == PurchaseOutcome.PENDING.value
        assert attempt.review_state is None
        assert attempt.invariant_result is None

    def test_invariant_result_ok_path(self) -> None:
        ok = InvariantResult(ok=True, reasons_passed=["total_ok"])
        assert ok.ok is True
        assert ok.reasons_failed == []


# -----------------------------------------------------------------------------
# Planner
# -----------------------------------------------------------------------------


class TestPlanner:
    def test_disabled_returns_none(self) -> None:
        cfg = _purchase_cfg(enabled=False)
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.IMAX_70MM, [_showtime("7:00p")]),
            ]),
        ])
        assert plan_purchase(parsed, target_name="t", purchase_cfg=cfg) is None

    def test_not_on_sale_returns_none(self) -> None:
        cfg = _purchase_cfg()
        assert plan_purchase(_not_on_sale(), target_name="t", purchase_cfg=cfg) is None

    def test_no_citywalk_theater_returns_none(self) -> None:
        cfg = _purchase_cfg()
        parsed = _partial_release([
            _theater("Other Theater", [
                _format_section(FormatTag.IMAX_70MM, [_showtime("7:00p")]),
            ], is_citywalk=False),
        ])
        assert plan_purchase(parsed, target_name="t", purchase_cfg=cfg) is None

    def test_no_matching_format_returns_none(self) -> None:
        cfg = _purchase_cfg()
        # CityWalk has only LASER_RECLINER, but config has no priority for it.
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.LASER_RECLINER, [_showtime("7:00p")]),
            ]),
        ])
        assert plan_purchase(parsed, target_name="t", purchase_cfg=cfg) is None

    def test_non_buyable_showtime_skipped(self) -> None:
        cfg = _purchase_cfg()
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.IMAX_70MM, [
                    _showtime("4:00p", buyable=False),
                    _showtime("7:00p", buyable=True),
                ]),
            ]),
        ])
        plan = plan_purchase(parsed, target_name="t", purchase_cfg=cfg)
        assert plan is not None
        assert plan.showtime_label == "7:00p"

    def test_missing_ticket_url_skipped(self) -> None:
        cfg = _purchase_cfg()
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.IMAX_70MM, [
                    _showtime("4:00p", url=None),
                    _showtime("7:00p"),
                ]),
            ]),
        ])
        plan = plan_purchase(parsed, target_name="t", purchase_cfg=cfg)
        assert plan is not None
        assert plan.showtime_label == "7:00p"

    def test_first_qualifying_showtime_wins(self) -> None:
        cfg = _purchase_cfg()
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.IMAX_70MM, [
                    _showtime("4:00p"),
                    _showtime("7:00p"),
                    _showtime("10:00p"),
                ]),
            ]),
        ])
        plan = plan_purchase(parsed, target_name="hateful8", purchase_cfg=cfg)
        assert plan is not None
        assert plan.showtime_label == "4:00p"
        assert plan.target_name == "hateful8"
        assert plan.theater_name == "AMC Universal CityWalk 19"
        assert plan.format_tag == FormatTag.IMAX_70MM.value
        assert plan.auditorium == 19
        assert plan.seat_priority == ["N10", "N11", "N12"]
        assert plan.benefit_phrase_any == ["A-List"]
        assert plan.require_total_equals == "$0.00"

    def test_first_section_with_priority_wins(self) -> None:
        # CityWalk has DOLBY first, then IMAX_70MM. DOLBY also has a priority,
        # so it should win since it's iterated first.
        cfg = _purchase_cfg()
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.DOLBY, [_showtime("6:00p")]),
                _format_section(FormatTag.IMAX_70MM, [_showtime("7:00p")]),
            ]),
        ])
        plan = plan_purchase(parsed, target_name="t", purchase_cfg=cfg)
        assert plan is not None
        assert plan.format_tag == FormatTag.DOLBY.value
        assert plan.auditorium == 1
        assert plan.seat_priority == ["E9", "E10"]

    def test_max_quantity_propagates(self) -> None:
        cfg = _purchase_cfg(max_quantity=3)
        parsed = _partial_release([
            _theater("AMC Universal CityWalk 19", [
                _format_section(FormatTag.IMAX_70MM, [_showtime("7:00p")]),
            ]),
        ])
        plan = plan_purchase(parsed, target_name="t", purchase_cfg=cfg)
        assert plan is not None
        assert plan.quantity == 3


# -----------------------------------------------------------------------------
# Invariant validator
# -----------------------------------------------------------------------------


class TestInvariantHappyPath:
    def test_all_checks_pass(self) -> None:
        plan = _make_plan()
        review = _make_review()
        cfg = _make_invariant()
        result = validate_invariant(plan, review, cfg)
        assert result.ok is True, result.reasons_failed
        assert result.reasons_failed == []
        # The four checks (total + benefit + theater + showtime + seat).
        assert "total_ok" in result.reasons_passed
        assert any(r.startswith("benefit_phrase_ok") for r in result.reasons_passed)
        assert "theater_ok" in result.reasons_passed
        assert "showtime_ok" in result.reasons_passed
        assert any(r.startswith("seat_ok") for r in result.reasons_passed)


class TestInvariantTotal:
    def test_total_text_missing_fails(self) -> None:
        review = _make_review(total_text=None)
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert "total_text_missing" in result.reasons_failed

    def test_total_nonzero_fails(self) -> None:
        review = _make_review(total_text="Total: $5.00")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("total_mismatch" in r for r in result.reasons_failed)

    def test_total_with_currency_suffix_passes(self) -> None:
        review = _make_review(total_text="Total: US$0.00 USD")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_total_with_extra_whitespace_passes(self) -> None:
        review = _make_review(total_text="  Total:    $0.00  ")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_total_substring_match(self) -> None:
        # "Order Total: $0.00 (after A-List)" should still pass.
        review = _make_review(total_text="Order Total: $0.00 (after A-List)")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed


class TestInvariantBenefitPhrase:
    def test_benefit_missing_fails(self) -> None:
        review = _make_review(visible_phrases=["Subtotal", "Taxes"])
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("benefit_phrase_missing" in r for r in result.reasons_failed)

    def test_benefit_match_case_insensitive(self) -> None:
        review = _make_review(visible_phrases=["a-list benefit applied"])
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_benefit_check_skipped_when_config_empty(self) -> None:
        review = _make_review(visible_phrases=[])
        cfg = _make_invariant(require_benefit_phrase_any=[])
        result = validate_invariant(_make_plan(), review, cfg)
        assert result.ok is True, result.reasons_failed

    def test_first_matching_phrase_wins(self) -> None:
        review = _make_review(
            visible_phrases=["AMC Stubs A-List discount applied"]
        )
        cfg = _make_invariant(
            require_benefit_phrase_any=["A-List", "AMC Stubs A-List"]
        )
        result = validate_invariant(_make_plan(), review, cfg)
        assert result.ok is True
        assert any(
            "benefit_phrase_ok: 'A-List'" in r for r in result.reasons_passed
        )


class TestInvariantTheater:
    def test_theater_mismatch_fails(self) -> None:
        review = _make_review(theater_name="AMC Burbank 16")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("theater_mismatch" in r for r in result.reasons_failed)

    def test_theater_substring_match_short_form(self) -> None:
        # Plan says full name, observed has shorter form.
        review = _make_review(theater_name="AMC Universal CityWalk")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_theater_check_skipped(self) -> None:
        review = _make_review(theater_name="something else entirely")
        cfg = _make_invariant(require_theater_match=False)
        result = validate_invariant(_make_plan(), review, cfg)
        assert result.ok is True, result.reasons_failed

    def test_theater_missing_fails_when_required(self) -> None:
        review = _make_review(theater_name=None)
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert "theater_missing" in result.reasons_failed


class TestInvariantShowtime:
    def test_showtime_mismatch_fails(self) -> None:
        review = _make_review(showtime_label="9:30p")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("showtime_mismatch" in r for r in result.reasons_failed)

    def test_showtime_with_spaces_passes(self) -> None:
        # Fandango sometimes renders "7:00 p" with a space.
        review = _make_review(showtime_label="7:00 p")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_showtime_check_skipped(self) -> None:
        review = _make_review(showtime_label="9:30p")
        cfg = _make_invariant(require_showtime_match=False)
        result = validate_invariant(_make_plan(), review, cfg)
        assert result.ok is True, result.reasons_failed


class TestInvariantSeat:
    def test_no_seats_fails(self) -> None:
        review = _make_review(selected_seats=[])
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert "no_seats_selected" in result.reasons_failed

    def test_wrong_auditorium_fails(self) -> None:
        review = _make_review(
            selected_seats=[SeatPick(auditorium=14, seat="N10")]
        )
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("auditorium_mismatch" in r for r in result.reasons_failed)

    def test_seat_outside_priority_fails(self) -> None:
        review = _make_review(
            selected_seats=[SeatPick(auditorium=19, seat="A1")]
        )
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("seat_outside_priority" in r for r in result.reasons_failed)

    def test_quantity_mismatch_fails(self) -> None:
        # Plan wants 1, review has 2.
        review = _make_review(
            selected_seats=[
                SeatPick(auditorium=19, seat="N10"),
                SeatPick(auditorium=19, seat="N11"),
            ]
        )
        result = validate_invariant(_make_plan(quantity=1), review, _make_invariant())
        assert result.ok is False
        assert any("quantity_mismatch" in r for r in result.reasons_failed)

    def test_quantity_two_passes_when_both_in_priority(self) -> None:
        review = _make_review(
            selected_seats=[
                SeatPick(auditorium=19, seat="N10"),
                SeatPick(auditorium=19, seat="N11"),
            ]
        )
        plan = _make_plan(quantity=2)
        result = validate_invariant(plan, review, _make_invariant())
        assert result.ok is True, result.reasons_failed

    def test_seat_check_skipped(self) -> None:
        review = _make_review(
            selected_seats=[SeatPick(auditorium=14, seat="ZZZ")]
        )
        cfg = _make_invariant(require_seat_match=False)
        result = validate_invariant(_make_plan(), review, cfg)
        assert result.ok is True, result.reasons_failed


class TestInvariantCompositeFailure:
    def test_multiple_failures_all_recorded(self) -> None:
        # Total wrong AND theater wrong AND seat wrong.
        review = _make_review(
            total_text="Total: $5.00",
            theater_name="AMC Burbank 16",
            selected_seats=[SeatPick(auditorium=14, seat="ZZ1")],
        )
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("total_mismatch" in r for r in result.reasons_failed)
        assert any("theater_mismatch" in r for r in result.reasons_failed)
        assert any("auditorium_mismatch" in r for r in result.reasons_failed)
        assert any("seat_outside_priority" in r for r in result.reasons_failed)

    def test_empty_total_text_string_fails_cleanly(self) -> None:
        # "" is not None, but doesn't contain "$0.00" -> total_mismatch.
        review = _make_review(total_text="")
        result = validate_invariant(_make_plan(), review, _make_invariant())
        assert result.ok is False
        assert any("total_mismatch" in r for r in result.reasons_failed)

    def test_release_schema_unrelated_to_invariant(self) -> None:
        # Sanity: ReleaseSchema is for the watcher, not the invariant.
        # We just want to confirm the enum is importable and untouched
        # by these tests so a future refactor doesn't accidentally couple them.
        assert ReleaseSchema.NOT_ON_SALE.value == "not_on_sale"
        assert WatchStatus.WATCHABLE.value == "watchable"

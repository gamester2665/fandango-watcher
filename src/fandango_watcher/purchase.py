"""Purchase planning + the ``$0.00`` A-List safety invariant.

This module is intentionally browser-free. Two responsibilities:

1. ``plan_purchase`` consumes a classified ``ParsedPageData`` plus the
   ``PurchaseConfig`` and emits a ``PurchasePlan`` describing exactly what
   the (Phase 4) scripted purchaser should attempt to buy: which CityWalk
   theater, which showtime URL, which auditorium, and the priority-ordered
   list of seats to try.

2. ``validate_invariant`` is the **hard kill switch**. The scripted purchaser
   MUST call it against the parsed Fandango review page right before
   clicking "Complete Reservation". If it returns ``ok=False``, the click
   is suppressed and the attempt is halted with ``HALTED_INVARIANT``.

Both functions are pure and exhaustively unit-tested in
``tests/test_purchase.py`` — the click-flow orchestrator (``purchaser.py``,
Phase 4) consumes them but does not duplicate their logic.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .config import InvariantConfig, PurchaseConfig
from .models import (
    FormatTag,
    FullReleasePageData,
    ParsedPageData,
    PartialReleasePageData,
)

# -----------------------------------------------------------------------------
# Outcomes
# -----------------------------------------------------------------------------


class PurchaseOutcome(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    HELD_FOR_CONFIRM = "held_for_confirm"
    HALTED_INVARIANT = "halted_invariant"
    HALTED_PREFERRED_SOLD_OUT = "halted_preferred_sold_out"
    HALTED_NO_MATCHING_SHOWTIME = "halted_no_matching_showtime"
    HALTED_DISABLED = "halted_disabled"
    FAILED_SCRIPTED = "failed_scripted"
    FAILED_AGENT_FALLBACK = "failed_agent_fallback"


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------


class _PurchaseModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SeatPick(_PurchaseModelBase):
    """One concrete seat the user holds or has been assigned."""

    auditorium: int = Field(ge=1)
    seat: str = Field(min_length=1)


class PurchasePlan(_PurchaseModelBase):
    """What the purchaser intends to buy. Persisted alongside each attempt."""

    target_name: str
    theater_name: str
    showtime_label: str
    showtime_url: str
    format_tag: FormatTag
    auditorium: int = Field(ge=1)
    seat_priority: list[str] = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)
    benefit_phrase_any: list[str] = Field(default_factory=list)
    require_total_equals: str = "$0.00"


class ReviewPageState(_PurchaseModelBase):
    """Everything the scripted purchaser must extract from Fandango's review
    page so the invariant validator can decide whether to click Complete.

    Fields are deliberately raw text so the validator can normalize on its
    own terms (different Fandango cohorts render the total as ``$0.00``,
    ``$0.00 USD``, ``Total: $0.00``, etc.).
    """

    theater_name: str | None = None
    showtime_label: str | None = None
    selected_seats: list[SeatPick] = Field(default_factory=list)
    total_text: str | None = None
    visible_phrases: list[str] = Field(default_factory=list)
    quantity: int | None = None


class InvariantResult(_PurchaseModelBase):
    """Outcome of :func:`validate_invariant`. Click only on ``ok=True``."""

    ok: bool
    reasons_failed: list[str] = Field(default_factory=list)
    reasons_passed: list[str] = Field(default_factory=list)


class PurchaseAttempt(_PurchaseModelBase):
    """Audit record of one end-to-end purchase attempt."""

    plan: PurchasePlan
    started_at: datetime
    finished_at: datetime | None = None
    outcome: PurchaseOutcome = PurchaseOutcome.PENDING
    review_state: ReviewPageState | None = None
    invariant_result: InvariantResult | None = None
    halt_reason: str | None = None
    screenshots: list[str] = Field(default_factory=list)
    error_message: str | None = None
    agent_rescue_attempted: bool = False
    agent_rescue_outcome: str | None = None
    agent_rescue_notes: str | None = None


# -----------------------------------------------------------------------------
# Planner
# -----------------------------------------------------------------------------


def _format_tag_str(value: FormatTag | str) -> str:
    """Return the stringified format tag regardless of enum vs str input.

    With ``use_enum_values=True`` Pydantic stores the ``.value`` string on
    the model, but in tests we sometimes construct snapshots with the enum
    directly. Normalize so dict lookups always match.
    """
    return getattr(value, "value", value)


def plan_purchase(
    parsed: ParsedPageData,
    *,
    target_name: str,
    purchase_cfg: PurchaseConfig,
) -> PurchasePlan | None:
    """Pick the highest-priority CityWalk showtime + seats for ``parsed``.

    Returns ``None`` when:

    * Purchase is disabled in config
    * Page is ``not_on_sale`` (no theaters / showtimes)
    * No CityWalk theater is present
    * No CityWalk showtime matches a configured seat-priority format
    * The matching showtime has no ticket URL or is not buyable

    The first qualifying showtime wins. We do NOT try alternate showtimes
    when the preferred seats are sold out — ``cfg.purchase.on_preferred_sold_out``
    governs that behavior at the purchaser level (default ``notify_only``).
    """
    if not purchase_cfg.enabled:
        return None

    if not isinstance(parsed, (PartialReleasePageData, FullReleasePageData)):
        return None

    for theater in parsed.theaters:
        if not theater.is_citywalk:
            continue
        for fs in theater.format_sections:
            fmt_str = _format_tag_str(fs.normalized_format)
            seat_pref = purchase_cfg.seat_priority.get(fmt_str)
            if seat_pref is None:
                continue
            for st in fs.showtimes:
                if not st.is_buyable or not st.ticket_url:
                    continue
                return PurchasePlan(
                    target_name=target_name,
                    theater_name=theater.name,
                    showtime_label=st.label,
                    showtime_url=st.ticket_url,
                    format_tag=FormatTag(fmt_str),
                    auditorium=seat_pref.auditorium,
                    seat_priority=list(seat_pref.seats),
                    quantity=purchase_cfg.max_quantity,
                    benefit_phrase_any=list(
                        purchase_cfg.invariant.require_benefit_phrase_any
                    ),
                    require_total_equals=purchase_cfg.invariant.require_total_equals,
                )
    return None


# -----------------------------------------------------------------------------
# Invariant validator
# -----------------------------------------------------------------------------


def _normalize_money(text: str) -> str:
    """Lowercase, collapse whitespace, normalize currency prefixes."""
    collapsed = " ".join(text.split()).lower()
    return collapsed.replace("us$", "$")


def _theater_matches(plan_theater: str, observed: str) -> bool:
    p = " ".join(plan_theater.split()).lower()
    o = " ".join(observed.split()).lower()
    return p in o or o in p


def _showtime_matches(plan_label: str, observed: str) -> bool:
    p = plan_label.strip().lower().replace(" ", "")
    o = observed.strip().lower().replace(" ", "")
    return p in o or o in p


def validate_invariant(
    plan: PurchasePlan,
    review: ReviewPageState,
    invariant_cfg: InvariantConfig,
) -> InvariantResult:
    """Re-read the review page state and decide whether the click is safe.

    Checks (in order):

    1. ``review.total_text`` contains ``invariant_cfg.require_total_equals``
       after whitespace + currency-prefix normalization. Hard fail otherwise.
    2. At least one of ``invariant_cfg.require_benefit_phrase_any`` appears
       (case-insensitive substring) in ``review.visible_phrases``. Skipped
       if the list is empty.
    3. If ``require_theater_match``: ``review.theater_name`` matches
       ``plan.theater_name`` (substring either direction, whitespace
       collapsed) — Fandango sometimes renders the short or long form.
    4. If ``require_showtime_match``: ``review.showtime_label`` matches
       ``plan.showtime_label`` (substring either direction, spaces stripped
       so ``"7:00 p"`` matches ``"7:00p"``).
    5. If ``require_seat_match``:
       a. ``review.selected_seats`` is non-empty.
       b. Every selected seat's auditorium equals ``plan.auditorium``.
       c. Every selected seat label appears in ``plan.seat_priority``.
       d. The selected-seat count equals ``plan.quantity``.

    Any failure adds a string to ``reasons_failed`` and forces ``ok=False``.
    Successful checks are recorded in ``reasons_passed`` for the audit log.
    """
    failed: list[str] = []
    passed: list[str] = []

    # 1. Total --------------------------------------------------------------
    if review.total_text is None:
        failed.append("total_text_missing")
    else:
        want = _normalize_money(invariant_cfg.require_total_equals)
        got = _normalize_money(review.total_text)
        if want not in got:
            failed.append(f"total_mismatch: want {want!r} in {got!r}")
        else:
            passed.append("total_ok")

    # 2. Benefit phrase -----------------------------------------------------
    if invariant_cfg.require_benefit_phrase_any:
        normalized_visible = [v.lower() for v in review.visible_phrases]
        match: str | None = None
        for needle in invariant_cfg.require_benefit_phrase_any:
            n = needle.lower()
            if any(n in haystack for haystack in normalized_visible):
                match = needle
                break
        if match is None:
            failed.append(
                "benefit_phrase_missing: none of "
                f"{invariant_cfg.require_benefit_phrase_any!r} found"
            )
        else:
            passed.append(f"benefit_phrase_ok: {match!r}")

    # 3. Theater ------------------------------------------------------------
    if invariant_cfg.require_theater_match:
        if review.theater_name is None:
            failed.append("theater_missing")
        elif not _theater_matches(plan.theater_name, review.theater_name):
            failed.append(
                f"theater_mismatch: plan={plan.theater_name!r} "
                f"observed={review.theater_name!r}"
            )
        else:
            passed.append("theater_ok")

    # 4. Showtime -----------------------------------------------------------
    if invariant_cfg.require_showtime_match:
        if review.showtime_label is None:
            failed.append("showtime_missing")
        elif not _showtime_matches(plan.showtime_label, review.showtime_label):
            failed.append(
                f"showtime_mismatch: plan={plan.showtime_label!r} "
                f"observed={review.showtime_label!r}"
            )
        else:
            passed.append("showtime_ok")

    # 5. Seats --------------------------------------------------------------
    if invariant_cfg.require_seat_match:
        if not review.selected_seats:
            failed.append("no_seats_selected")
        else:
            wrong_aud = [
                p for p in review.selected_seats if p.auditorium != plan.auditorium
            ]
            extras = [
                p.seat for p in review.selected_seats
                if p.seat not in plan.seat_priority
            ]
            if wrong_aud:
                failed.append(
                    f"auditorium_mismatch: plan={plan.auditorium} "
                    f"observed={[(p.auditorium, p.seat) for p in wrong_aud]}"
                )
            if extras:
                failed.append(
                    f"seat_outside_priority: extras={extras} "
                    f"plan_priority={plan.seat_priority}"
                )
            if len(review.selected_seats) != plan.quantity:
                failed.append(
                    f"quantity_mismatch: plan={plan.quantity} "
                    f"observed={len(review.selected_seats)}"
                )
            if not (wrong_aud or extras) and len(
                review.selected_seats
            ) == plan.quantity:
                passed.append(
                    f"seat_ok: {[p.seat for p in review.selected_seats]}"
                )

    return InvariantResult(
        ok=not failed, reasons_failed=failed, reasons_passed=passed
    )

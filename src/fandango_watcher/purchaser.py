"""Scripted Playwright checkout for a :class:`~.purchase.PurchasePlan`.

Selectors are intentionally broad (roles, ``data-*``, substring text) so we
can tighten them once live Fandango DOM samples land. The **only** authority
for whether to click *Complete* is :func:`~.purchase.validate_invariant` —
this module never bypasses it.

``run_scripted_purchase`` is dependency-injected at the watch-loop boundary
(``purchase_fn=``) so unit tests can substitute a stub without Playwright.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page
from playwright.sync_api import sync_playwright

from .config import BrowserConfig, PurchaseConfig
from .purchase import (
    InvariantResult,
    PurchaseAttempt,
    PurchaseOutcome,
    PurchasePlan,
    ReviewPageState,
    SeatPick,
    validate_invariant,
)

logger = logging.getLogger(__name__)

_REVIEW_SNAPSHOT_JS = """
() => ({
  bodyText: (document.body && document.body.innerText) || "",
  title: document.title || "",
})
"""


def _attempt_dir(per_purchase_root: Path | None) -> Path | None:
    if per_purchase_root is None:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = per_purchase_root / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _screenshot(page: Page, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def extract_review_state(plan: PurchasePlan, snapshot: dict[str, Any]) -> ReviewPageState:
    """Best-effort review DOM → :class:`~.purchase.ReviewPageState`.

    Fandango markup varies; this favors false negatives (invariant fails safe)
    over false positives on ``$0.00``.
    """
    body: str = snapshot.get("bodyText") or ""
    title: str = snapshot.get("title") or ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    visible_phrases = lines if len(lines) <= 120 else [body[:50_000]]

    total_text: str | None = None
    for line in lines:
        if re.search(r"\$\s*\d+[.,]\d{2}", line) and re.search(
            r"total|order\s*total|amount\s*due|today'?s\s*total",
            line,
            re.I,
        ):
            total_text = line
            break
    if total_text is None:
        m = re.search(
            r"([^\n]{0,60}\$\s*0(?:[.,]00)?(?:\s*USD)?)",
            body,
            re.I,
        )
        if m:
            total_text = m.group(1).strip()

    theater_name: str | None = None
    if plan.theater_name and plan.theater_name.lower() in body.lower():
        theater_name = plan.theater_name
    else:
        for ln in lines:
            if "citywalk" in ln.lower() or "amc universal" in ln.lower():
                theater_name = ln
                break

    showtime_label: str | None = None
    if plan.showtime_label and plan.showtime_label.lower() in body.lower():
        showtime_label = plan.showtime_label
    elif plan.showtime_label:
        compact = re.sub(r"\s+", "", plan.showtime_label.lower())
        compact_body = re.sub(r"\s+", "", body.lower())
        if compact and compact in compact_body:
            showtime_label = plan.showtime_label

    selected: list[SeatPick] = []
    for seat in plan.seat_priority:
        if not seat:
            continue
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(seat)}(?![A-Za-z0-9])",
            body,
            re.I,
        ):
            selected.append(SeatPick(auditorium=plan.auditorium, seat=seat))

    return ReviewPageState(
        theater_name=theater_name,
        showtime_label=showtime_label,
        selected_seats=selected[: max(plan.quantity, 1)],
        total_text=total_text,
        visible_phrases=visible_phrases,
        quantity=plan.quantity,
    )


def _click_seat(page: Page, seat: str, timeout_ms: int) -> bool:
    """Try several locator strategies; return True if a click was issued."""
    candidates = [
        f'[data-testid*="{seat}" i]',
        f'[data-seat*="{seat}" i]',
        f'[aria-label*="{seat}" i]',
        f'[class*="seat" i][class*="{seat}" i]',
        f"button:has-text(\"{seat}\")",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            loc.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _advance_toward_review(page: Page, *, max_clicks: int = 10) -> None:
    """Click through common post-seat-picker CTAs (Continue / Next / Review)."""
    label = re.compile(
        r"continue|next|proceed|select(\s+|\s*)tickets|checkout|view\s*cart|"
        r"review(\s+|\s*)order",
        re.I,
    )
    for _ in range(max_clicks):
        btn = page.get_by_role("button", name=label).first
        try:
            if not btn.is_visible(timeout=800):
                break
        except Exception:
            break
        try:
            btn.click(timeout=5000)
            page.wait_for_timeout(350)
        except Exception:
            break


def _click_complete_reservation(page: Page) -> bool:
    patterns = (
        re.compile(r"complete\s*(my\s*)?reservation", re.I),
        re.compile(r"^\s*purchase\s*$", re.I),
        re.compile(r"place\s*(my\s*)?order", re.I),
        re.compile(r"confirm\s*(my\s*)?purchase", re.I),
    )
    for pat in patterns:
        loc = page.get_by_role("button", name=pat).first
        try:
            loc.click(timeout=12_000)
            return True
        except Exception:
            continue
    return False


@contextmanager
def _browser_session(
    browser_cfg: BrowserConfig,
) -> Iterator[tuple[object, BrowserContext, Browser | None]]:
    """Mirror ``watcher.crawl_target`` launch semantics (persistent vs fresh)."""
    profile_path = Path(browser_cfg.user_data_dir)
    use_persistent = profile_path.exists() and any(profile_path.iterdir())

    context_kwargs: dict[str, Any] = {
        "locale": browser_cfg.locale,
        "timezone_id": browser_cfg.timezone,
        "viewport": {
            "width": browser_cfg.viewport.width,
            "height": browser_cfg.viewport.height,
        },
    }

    with sync_playwright() as pw:
        if use_persistent:
            context = pw.chromium.launch_persistent_context(
                str(profile_path),
                headless=browser_cfg.headless,
                **context_kwargs,
            )
            browser: Browser | None = None
        else:
            browser = pw.chromium.launch(headless=browser_cfg.headless)
            context = browser.new_context(**context_kwargs)

        try:
            yield pw, context, browser
        finally:
            context.close()
            if browser is not None:
                browser.close()


def run_scripted_purchase(
    plan: PurchasePlan,
    *,
    browser_cfg: BrowserConfig,
    purchase_cfg: PurchaseConfig,
    per_purchase_dir: Path | None = None,
    hold_for_confirm: bool = False,
    navigate_timeout_ms: int = 90_000,
    seat_click_timeout_ms: int = 3_500,
    browser_session: Any | None = None,
) -> PurchaseAttempt:
    """Run checkout for ``plan``; gate final click on :func:`~.purchase.validate_invariant`.

    ``hold_for_confirm`` stops after a passing invariant (no Complete click).

    ``browser_session`` is an optional context manager yielding
    ``(pw, context, browser)`` like :func:`_browser_session` for tests.
    """
    started_at = datetime.now(UTC)
    shots: list[str] = []
    attempt_dir = _attempt_dir(per_purchase_dir)

    def _finish(
        outcome: PurchaseOutcome,
        *,
        review: ReviewPageState | None = None,
        inv: InvariantResult | None = None,
        halt_reason: str | None = None,
        err: str | None = None,
    ) -> PurchaseAttempt:
        return PurchaseAttempt(
            plan=plan,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            outcome=outcome,
            review_state=review,
            invariant_result=inv,
            halt_reason=halt_reason,
            screenshots=shots,
            error_message=err,
        )

    session_cm = (
        browser_session if browser_session is not None else _browser_session(browser_cfg)
    )

    try:
        with session_cm as (_pw, context, _browser):
            page = context.new_page()
            step = 0

            def snap(label: str) -> None:
                nonlocal step
                if attempt_dir is None:
                    return
                step += 1
                p = attempt_dir / f"{step:02d}-{label}.png"
                shots.append(_screenshot(page, p))

            page.goto(
                plan.showtime_url,
                wait_until="domcontentloaded",
                timeout=navigate_timeout_ms,
            )
            page.wait_for_timeout(1200)
            snap("after-goto")

            picked = False
            for seat in plan.seat_priority:
                if _click_seat(page, seat, seat_click_timeout_ms):
                    picked = True
                    logger.info("selected seat candidate=%s", seat)
                    break
            snap("after-seat-click")

            if not picked:
                return _finish(
                    PurchaseOutcome.HALTED_PREFERRED_SOLD_OUT,
                    halt_reason="no preferred seat could be clicked",
                )

            _advance_toward_review(page)
            snap("after-advance")

            raw = page.evaluate(_REVIEW_SNAPSHOT_JS)
            if not isinstance(raw, dict):
                raw = {}
            review = extract_review_state(plan, raw)
            inv = validate_invariant(plan, review, purchase_cfg.invariant)
            snap("review-before-decision")

            if not inv.ok:
                return _finish(
                    PurchaseOutcome.HALTED_INVARIANT,
                    review=review,
                    inv=inv,
                    halt_reason="; ".join(inv.reasons_failed),
                )

            if hold_for_confirm:
                return _finish(
                    PurchaseOutcome.HELD_FOR_CONFIRM,
                    review=review,
                    inv=inv,
                    halt_reason="hold_for_confirm: invariant passed; complete manually",
                )

            if not _click_complete_reservation(page):
                return _finish(
                    PurchaseOutcome.FAILED_SCRIPTED,
                    review=review,
                    inv=inv,
                    err="complete reservation button not found or not clickable",
                )
            snap("after-complete-click")
            page.wait_for_timeout(2000)

            raw2 = page.evaluate(_REVIEW_SNAPSHOT_JS)
            review2 = extract_review_state(plan, raw2 if isinstance(raw2, dict) else {})
            inv2 = validate_invariant(plan, review2, purchase_cfg.invariant)
            if inv2.ok:
                return _finish(PurchaseOutcome.SUCCESS, review=review2, inv=inv2)

            # Post-click DOM drift: still treat as success if URL/title hints confirmation,
            # but prefer a passing second invariant read.
            url = page.url.lower()
            body = (raw2.get("bodyText") if isinstance(raw2, dict) else "") or ""
            if "confirmation" in url or "confirm" in url or "success" in body.lower():
                return _finish(PurchaseOutcome.SUCCESS, review=review2, inv=inv2)

            return _finish(
                PurchaseOutcome.HALTED_INVARIANT,
                review=review2,
                inv=inv2,
                halt_reason="post-click invariant failed: " + "; ".join(inv2.reasons_failed),
            )

    except Exception as e:  # noqa: BLE001 — surface any Playwright failure
        logger.exception("scripted purchase crashed")
        return _finish(
            PurchaseOutcome.FAILED_SCRIPTED,
            err=f"{type(e).__name__}: {e}",
        )

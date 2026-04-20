"""Playwright-driven Fandango crawler.

Exposes one public entry point, :func:`crawl_target`, which:

1. Launches Chromium via Playwright's sync API (persistent context if a
   ``user_data_dir`` exists on disk, fresh context otherwise).
2. Navigates to the target URL.
3. Optionally clicks a format chip (``TargetConfig.format_filter_click_*``)
   so the DOM matches a filtered view (e.g. IMAX 3D) before capture.
4. Dumps a timestamped screenshot to ``screenshot_dir`` (if given).
5. Extracts a :class:`~fandango_watcher.detect.PageSnapshot` via a small
   browser-side JS helper.
6. Hands the snapshot to :func:`~fandango_watcher.detect.classify` and
   returns the validated ``ParsedPageData``.

The DOM selectors used by the extractor are best-effort against Fandango's
layout and will need iteration once live pages are inspected; see PLAN.md
Phase 2 checklist.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import BrowserConfig, TargetConfig
from .detect import (
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
)
from .models import ParsedPageData, ReleaseSchema
from .playwright_video import rename_page_video_after_close

logger = logging.getLogger(__name__)

_EXTRACTOR_JS_CACHE: str | None = None


def _extractor_js() -> str:
    """Load shipped ``extract_page.js`` (same extractor as legacy inline string)."""
    global _EXTRACTOR_JS_CACHE
    if _EXTRACTOR_JS_CACHE is None:
        path = Path(__file__).with_name("extract_page.js")
        _EXTRACTOR_JS_CACHE = path.read_text(encoding="utf-8")
    return _EXTRACTOR_JS_CACHE


def _wait_for_fandango_showtime_dom(page: Page, *, timeout_ms: int = 15_000) -> None:
    """Block until movie-times UI has likely painted (or timeout).

    ``domcontentloaded`` plus a fixed sleep can snapshot a shell where neither
    legacy theater-cards nor ``h2.shared-theater-header__name`` have mounted
    yet, yielding 0 theaters and a false ``not_on_sale``. Waiting for a
    selector that appears on both layout families avoids flaky classification.
    """
    try:
        page.wait_for_selector(
            "h2.shared-theater-header__name, "
            "[data-testid*='theater-card'], "
            "[class*='TheaterCard' i], "
            "a[href*='ticketing']",
            timeout=timeout_ms,
        )
    except Exception:
        logger.debug(
            "showtime DOM wait timed out after %dms; extracting anyway",
            timeout_ms,
            exc_info=True,
        )


def _maybe_click_format_filter(page: Page, target: TargetConfig) -> None:
    """If configured, click a Fandango format chip before extraction.

    Use ``format_filter_click_selector`` for a CSS selector, or
    ``format_filter_click_label`` for a case-insensitive substring match
    (scoped to ``#lazyload-format-filters`` when present). On failure, logs a
    warning and continues so the crawl still returns a snapshot.
    """
    sel = target.format_filter_click_selector
    label = target.format_filter_click_label
    if not sel and not label:
        return
    timeout = int(target.format_filter_click_timeout_ms)
    try:
        if sel:
            logger.info(
                "format filter click (selector): target=%s selector=%r",
                target.name,
                sel,
            )
            page.locator(sel).first.click(timeout=timeout)
        else:
            assert label is not None
            logger.info(
                "format filter click (label): target=%s label=%r",
                target.name,
                label,
            )
            label_rx = re.compile(re.escape(label.strip()), re.IGNORECASE)
            bucket = page.locator("#lazyload-format-filters")
            if bucket.count() > 0:
                chip = bucket.locator("li, button, a, [role='button']").filter(
                    has_text=label_rx
                )
            else:
                chip = page.locator("[class*='format-filter__list-item' i]").filter(
                    has_text=label_rx
                )
            chip.first.click(timeout=timeout)
    except Exception:
        logger.warning(
            "format filter click failed for target=%s; extracting current DOM",
            target.name,
            exc_info=True,
        )
        return
    page.wait_for_timeout(800)
    _wait_for_fandango_showtime_dom(page, timeout_ms=min(12_000, timeout))


def _build_snapshot(
    *,
    page: Page,
    url: str,
    screenshot_path: Path | None,
) -> PageSnapshot:
    raw: dict[str, Any] = page.evaluate(_extractor_js())

    theaters = [
        ExtractedTheater(
            name=t["name"],
            address=t.get("address"),
            distance_miles=t.get("distance_miles"),
            format_sections=[
                ExtractedFormatSection(
                    label=fs["label"],
                    attributes=fs.get("attributes") or [],
                    showtimes=[
                        ExtractedShowtime(
                            label=s["label"],
                            ticket_url=s.get("ticket_url"),
                            is_buyable=bool(s.get("is_buyable", True)),
                            date_label=s.get("date_label"),
                        )
                        for s in fs.get("showtimes") or []
                    ],
                )
                for fs in t.get("format_sections") or []
            ],
        )
        for t in raw.get("theaters") or []
    ]

    return PageSnapshot(
        url=url,
        page_title=raw.get("page_title") or "",
        movie_title=raw.get("movie_title"),
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        format_filter_labels=raw.get("format_filter_labels") or [],
        theaters=theaters,
        fanalert_present=bool(raw.get("fanalert_present")),
        notify_me_present=bool(raw.get("notify_me_present")),
        loading_calendar_present=bool(raw.get("loading_calendar_present")),
        loading_format_filters_present=bool(raw.get("loading_format_filters_present")),
        ticket_url=raw.get("ticket_url"),
    )


def _screenshot_path_for(
    target_name: str, screenshot_dir: Path
) -> Path:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return screenshot_dir / f"{target_name}-{stamp}.png"


def _context_kwargs(browser_cfg: BrowserConfig) -> dict[str, Any]:
    return {
        "locale": browser_cfg.locale,
        "timezone_id": browser_cfg.timezone,
        "viewport": {
            "width": browser_cfg.viewport.width,
            "height": browser_cfg.viewport.height,
        },
        **browser_cfg.playwright_video_options(),
    }


def _open_context(
    pw: Any, browser_cfg: BrowserConfig
) -> tuple[BrowserContext, Browser | None]:
    """Launch persistent or ephemeral Chromium context (mirrors prior ``crawl_target``)."""
    profile_path = Path(browser_cfg.user_data_dir)
    use_persistent = profile_path.exists() and any(profile_path.iterdir())
    kwargs = _context_kwargs(browser_cfg)
    if use_persistent:
        context = pw.chromium.launch_persistent_context(
            str(profile_path),
            headless=browser_cfg.headless,
            **kwargs,
        )
        return context, None
    browser = pw.chromium.launch(headless=browser_cfg.headless)
    return browser.new_context(**kwargs), browser


def crawl_open_page(
    page: Page,
    target: TargetConfig,
    *,
    citywalk_anchor: str,
    screenshot_dir: Path | None,
    extra_wait_ms: int = 2500,
) -> ParsedPageData:
    """Navigate ``page`` to ``target`` and return classified data (no browser launch)."""
    page.goto(
        target.url,
        wait_until=target.wait_until,
        timeout=target.timeout_ms,
    )
    if extra_wait_ms > 0:
        page.wait_for_timeout(extra_wait_ms)
    _wait_for_fandango_showtime_dom(page)
    _maybe_click_format_filter(page, target)

    screenshot_path: Path | None = None
    if screenshot_dir is not None:
        screenshot_path = _screenshot_path_for(target.name, screenshot_dir)
        page.screenshot(path=str(screenshot_path), full_page=True)

    snapshot = _build_snapshot(
        page=page, url=target.url, screenshot_path=screenshot_path
    )
    parsed = classify(snapshot, citywalk_anchor=citywalk_anchor)
    if (
        parsed.release_schema == ReleaseSchema.NOT_ON_SALE
        and snapshot.ticket_url
        and "ticketing" in snapshot.ticket_url
    ):
        logger.info(
            "crawl_open_page: not_on_sale but ticketing URL present; "
            "waiting 4s and re-extracting once (slow showtime paint)"
        )
        page.wait_for_timeout(4000)
        snapshot = _build_snapshot(
            page=page, url=target.url, screenshot_path=screenshot_path
        )
        parsed = classify(snapshot, citywalk_anchor=citywalk_anchor)
    return parsed


def crawl_targets_in_tick(
    targets: list[TargetConfig],
    *,
    browser_cfg: BrowserConfig,
    citywalk_anchor: str,
    screenshot_dir: Path | None,
    extra_wait_ms: int = 2500,
) -> dict[str, ParsedPageData | BaseException]:
    """One Playwright sync session: single browser context, one page per target.

    Tracing (if enabled) records the whole tick into one ``watch-tick-*.zip``.
    Per-target failures become :class:`BaseException` values so other targets
    still run in the same tick.
    """
    if not targets:
        return {}
    out: dict[str, ParsedPageData | BaseException] = {}
    with sync_playwright() as pw:
        context, browser = _open_context(pw, browser_cfg)
        trace_dir = browser_cfg.trace_dir_path()
        if trace_dir is not None:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        tick_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            for target in targets:
                page = context.new_page()
                pstamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                try:
                    try:
                        out[target.name] = crawl_open_page(
                            page,
                            target,
                            citywalk_anchor=citywalk_anchor,
                            screenshot_dir=screenshot_dir,
                            extra_wait_ms=extra_wait_ms,
                        )
                    except Exception as e:  # noqa: BLE001 — isolate per-target
                        out[target.name] = e
                finally:
                    rename_page_video_after_close(
                        page,
                        browser_cfg=browser_cfg,
                        label=target.name,
                        stamp=pstamp,
                    )
                    page.close()
        finally:
            if trace_dir is not None:
                trace_path = trace_dir / f"watch-tick-{tick_stamp}.zip"
                try:
                    context.tracing.stop(path=str(trace_path))
                except Exception:  # noqa: BLE001
                    pass
            context.close()
            if browser is not None:
                browser.close()
    return out


def crawl_target(
    target: TargetConfig,
    *,
    browser_cfg: BrowserConfig,
    citywalk_anchor: str,
    screenshot_dir: Path | None = None,
    extra_wait_ms: int = 2500,
) -> ParsedPageData:
    """Crawl one Fandango target and return a validated ``ParsedPageData``.

    ``extra_wait_ms`` lets JS-rendered theater cards settle after
    ``wait_until`` fires. Increase if the page routinely ships empty cards.

    Uses one browser context and one page (full cold start). For the watch
    loop prefer :func:`crawl_targets_in_tick` to reuse the context across
    targets.
    """
    with sync_playwright() as pw:
        context, browser = _open_context(pw, browser_cfg)
        trace_dir = browser_cfg.trace_dir_path()
        if trace_dir is not None:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        page: Page | None = None
        try:
            page = context.new_page()
            parsed = crawl_open_page(
                page,
                target,
                citywalk_anchor=citywalk_anchor,
                screenshot_dir=screenshot_dir,
                extra_wait_ms=extra_wait_ms,
            )
        finally:
            if trace_dir is not None:
                trace_path = trace_dir / f"{target.name}-{stamp}.zip"
                try:
                    context.tracing.stop(path=str(trace_path))
                except Exception:  # noqa: BLE001 — never let tracing kill the crawl
                    pass
            if page is not None:
                rename_page_video_after_close(
                    page,
                    browser_cfg=browser_cfg,
                    label=target.name,
                    stamp=stamp,
                )
            context.close()
            if browser is not None:
                browser.close()

    return parsed

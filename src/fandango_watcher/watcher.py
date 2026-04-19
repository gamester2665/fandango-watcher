"""Playwright-driven Fandango crawler.

Exposes one public entry point, :func:`crawl_target`, which:

1. Launches Chromium via Playwright's sync API (persistent context if a
   ``user_data_dir`` exists on disk, fresh context otherwise).
2. Navigates to the target URL.
3. Dumps a timestamped screenshot to ``screenshot_dir`` (if given).
4. Extracts a :class:`~fandango_watcher.detect.PageSnapshot` via a small
   browser-side JS helper.
5. Hands the snapshot to :func:`~fandango_watcher.detect.classify` and
   returns the validated ``ParsedPageData``.

The DOM selectors used by the extractor are best-effort against Fandango's
layout and will need iteration once live pages are inspected; see PLAN.md
Phase 2 checklist.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from .config import BrowserConfig, TargetConfig
from .playwright_video import rename_page_video_after_close
from .detect import (
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
)
from .models import ReleaseSchema

logger = logging.getLogger(__name__)


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
from .models import ParsedPageData


# -----------------------------------------------------------------------------
# JS extractor. Kept inline so the whole data path is visible in one file.
#
# Returns a plain JSON-compatible dict shaped like PageSnapshot's extractor
# fields. All selectors are broad/substring-based because Fandango rotates
# utility class names; refine once we have captured DOM samples.
# -----------------------------------------------------------------------------

_EXTRACTOR_JS = r"""
() => {
  const text = (el) => (el && el.textContent ? el.textContent.trim() : "");
  const bodyText = (document.body && document.body.innerText) || "";

  // --- Positive/negative text signals -------------------------------------
  const fanalertPresent =
    /FanAlert|Notify Me/i.test(bodyText);
  const notifyMePresent = /Notify Me/i.test(bodyText);
  const loadingCalendarPresent = /Loading calendar/i.test(bodyText);
  const loadingFormatFiltersPresent = /Loading format filters/i.test(bodyText);

  // --- Format filter chips ------------------------------------------------
  const filterSelectors = [
    '[data-testid*="format-filter"]',
    '[class*="format-filter" i]',
    '[class*="FormatFilter"]',
    'button[aria-pressed][class*="format" i]',
  ];
  const filterEls = new Set();
  for (const sel of filterSelectors) {
    document.querySelectorAll(sel).forEach((el) => filterEls.add(el));
  }
  const formatFilterLabels = Array.from(filterEls)
    .map(text)
    .filter((s) => s && s.length <= 40);

  // --- Theater cards ------------------------------------------------------
  const cardSelectors = [
    '[data-testid*="theater-card"]',
    '[data-testid*="theater"]',
    '[class*="theater-card" i]',
    '[class*="TheaterCard"]',
    '[class*="theaterCard"]',
  ];
  const cardEls = new Set();
  for (const sel of cardSelectors) {
    document.querySelectorAll(sel).forEach((el) => cardEls.add(el));
  }

  const theaters = [];
  cardEls.forEach((card) => {
    const heading =
      card.querySelector(
        'h1, h2, h3, h4, [class*="theater-name" i], [class*="TheaterName"], [data-testid*="theater-name"]'
      ) || null;
    const name = text(heading);
    if (!name) return;

    // Format sections within the card.
    const sections = [];
    const sectionHeaderSelectors = [
      '[class*="format-header" i]',
      '[class*="FormatHeader"]',
      '[data-testid*="format-header"]',
      '[class*="format-section" i] > :first-child',
    ];
    const sectionHeaders = new Set();
    for (const sel of sectionHeaderSelectors) {
      card.querySelectorAll(sel).forEach((el) => sectionHeaders.add(el));
    }

    // Fall back: treat each "format"-ish container as a section if no
    // explicit headers exist. This keeps extraction non-empty on DOM drift.
    if (sectionHeaders.size === 0) {
      card
        .querySelectorAll('[class*="format" i], [data-testid*="format"]')
        .forEach((el) => {
          const label = text(el);
          if (label && label.length <= 60) sectionHeaders.add(el);
        });
    }

    sectionHeaders.forEach((hdr) => {
      const label = text(hdr);
      if (!label) return;

      const container =
        hdr.closest(
          '[class*="format-section" i], [class*="FormatSection"], [class*="showtimes-section" i]'
        ) || hdr.parentElement;

      const showtimes = [];
      if (container) {
        const showtimeEls = container.querySelectorAll(
          'a[href*="ticketing"], a[href*="buy"], a[class*="showtime" i], button[class*="showtime" i], [data-testid*="showtime"]'
        );
        showtimeEls.forEach((el) => {
          const label = text(el);
          if (!label) return;
          if (!/\d{1,2}:\d{2}/.test(label)) return;
          showtimes.push({
            label,
            ticket_url: el.href || null,
            is_buyable: !el.disabled && el.getAttribute("aria-disabled") !== "true",
            date_label: null,
          });
        });
      }

      sections.push({
        label,
        attributes: [],
        showtimes,
      });
    });

    theaters.push({
      name,
      address: null,
      distance_miles: null,
      format_sections: sections,
    });
  });

  // --- Fandango "shared showtimes" layout (2025+) -------------------------
  // Many movie-times pages use h2.shared-theater-header__name inside
  // .shared-showtimes__container. Those pages often have **no** elements
  // matching theater-card data-testids, so the legacy loop above yields
  // zero theaters and we mis-classify ticketed pages as not_on_sale.
  if (theaters.length === 0) {
    document
      .querySelectorAll(
        'h2.shared-theater-header__name, h3.shared-theater-header__name'
      )
      .forEach((heading) => {
        const name = text(heading);
        if (!name) return;
        const container =
          heading.closest('.shared-showtimes__container') ||
          heading.closest('[class*="shared-showtimes"]');
        if (!container) return;
        const showtimes = [];
        container.querySelectorAll('a').forEach((el) => {
          const lbl = text(el);
          if (!lbl) return;
          if (!/\d{1,2}:\d{2}/.test(lbl)) return;
          showtimes.push({
            label: lbl,
            ticket_url: el.href || null,
            is_buyable:
              !el.disabled && el.getAttribute('aria-disabled') !== 'true',
            date_label: null,
          });
        });
        // One theater with zero parsed times still yields partial_release
        // (theater_count > 0) vs not_on_sale; prefer real showtime rows when present.
        theaters.push({
          name,
          address: null,
          distance_miles: null,
          format_sections: [
            {
              label: 'Standard',
              attributes: [],
              showtimes,
            },
          ],
        });
      });
  }

  return {
    page_title: document.title || "",
    movie_title:
      text(document.querySelector('h1[class*="movie" i], h1[data-testid*="movie"], h1')) || null,
    format_filter_labels: Array.from(new Set(formatFilterLabels)),
    theaters,
    fanalert_present: fanalertPresent,
    notify_me_present: notifyMePresent,
    loading_calendar_present: loadingCalendarPresent,
    loading_format_filters_present: loadingFormatFiltersPresent,
    // The most prominent "Get Tickets"-style anchor, if any.
    ticket_url: (() => {
      const a = document.querySelector(
        'a[href*="ticketing"], a[href*="buy-tickets"], a[data-testid*="get-tickets"]'
      );
      return a ? a.href || null : null;
    })(),
  };
}
"""


def _build_snapshot(
    *,
    page: Page,
    url: str,
    screenshot_path: Path | None,
) -> PageSnapshot:
    raw: dict[str, Any] = page.evaluate(_EXTRACTOR_JS)

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
    """
    profile_path = Path(browser_cfg.user_data_dir)
    use_persistent = profile_path.exists() and any(profile_path.iterdir())

    context_kwargs: dict[str, Any] = {
        "locale": browser_cfg.locale,
        "timezone_id": browser_cfg.timezone,
        "viewport": {
            "width": browser_cfg.viewport.width,
            "height": browser_cfg.viewport.height,
        },
        **browser_cfg.playwright_video_options(),
    }

    with sync_playwright() as pw:
        if use_persistent:
            context = pw.chromium.launch_persistent_context(
                str(profile_path),
                headless=browser_cfg.headless,
                **context_kwargs,
            )
            browser = None
        else:
            browser = pw.chromium.launch(headless=browser_cfg.headless)
            context = browser.new_context(**context_kwargs)

        trace_dir = browser_cfg.trace_dir_path()
        if trace_dir is not None:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        page: Page | None = None
        try:
            page = context.new_page()
            page.goto(
                target.url,
                wait_until=target.wait_until,
                timeout=target.timeout_ms,
            )
            if extra_wait_ms > 0:
                page.wait_for_timeout(extra_wait_ms)
            _wait_for_fandango_showtime_dom(page)

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
                    "crawl_target: not_on_sale but ticketing URL present; "
                    "waiting 4s and re-extracting once (slow showtime paint)"
                )
                page.wait_for_timeout(4000)
                snapshot = _build_snapshot(
                    page=page, url=target.url, screenshot_path=screenshot_path
                )
                parsed = classify(snapshot, citywalk_anchor=citywalk_anchor)
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

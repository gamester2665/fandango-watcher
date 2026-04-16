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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from .config import BrowserConfig, TargetConfig
from .detect import (
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
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
    extra_wait_ms: int = 1500,
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

        try:
            page = context.new_page()
            page.goto(
                target.url,
                wait_until=target.wait_until,
                timeout=target.timeout_ms,
            )
            if extra_wait_ms > 0:
                page.wait_for_timeout(extra_wait_ms)

            screenshot_path: Path | None = None
            if screenshot_dir is not None:
                screenshot_path = _screenshot_path_for(target.name, screenshot_dir)
                page.screenshot(path=str(screenshot_path), full_page=True)

            snapshot = _build_snapshot(
                page=page, url=target.url, screenshot_path=screenshot_path
            )
        finally:
            context.close()
            if browser is not None:
                browser.close()

    return classify(snapshot, citywalk_anchor=citywalk_anchor)

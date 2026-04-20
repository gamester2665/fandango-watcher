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

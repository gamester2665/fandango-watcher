() => {
  const text = (el) => (el && el.textContent ? el.textContent.trim() : "");
  const isShowtimeLabel = (label) =>
    /\d{1,2}:\d{2}(\s*[ap]\.?m?\.?|[ap])/i.test(label || "");

  const isShowtimeBuyable = (el, label) => {
    if (!el) return false;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
    if (
      el.classList &&
      (el.classList.contains("showtime-btn--restricted") ||
        el.classList.contains("showtime-btn--sold-out") ||
        el.classList.contains("showtime-btn--unavailable"))
    ) {
      return false;
    }
    const labelText = label || text(el);
    const hint = (
      labelText +
      " " +
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.getAttribute("title") || "")
    ).toLowerCase();
    if (
      hint.includes("coming soon") ||
      hint.includes("not available") ||
      hint.includes("notify me") ||
      hint.includes("get notified")
    ) {
      return false;
    }
    const href = el.href || el.getAttribute("href") || "";
    if (
      href &&
      !/ticketing|\/buy/i.test(href) &&
      /\d{1,2}:\d{2}/.test(labelText)
    ) {
      return false;
    }
    return true;
  };

  const collectShowtimeElements = (root) => {
    if (!root) return [];
    const selector = [
      "span.showtime-btn",
      "button.showtime-btn",
      "a.showtime-btn",
      'a[href*="ticketing"]',
      'a[href*="buy"]',
      'a[class*="showtime" i]',
      'button[class*="showtime" i]',
      '[data-testid*="showtime"]',
    ].join(", ");
    return Array.from(root.querySelectorAll(selector));
  };

  const showtimesFromElements = (elements) => {
    const showtimes = [];
    elements.forEach((el) => {
      const label = text(el);
      if (!label || !isShowtimeLabel(label)) return;
      showtimes.push({
        label,
        ticket_url: el.href || el.getAttribute("href") || null,
        is_buyable: isShowtimeBuyable(el, label),
        date_label: null,
      });
    });
    return showtimes;
  };

  const showtimeCountForTheaters = (theaterList) =>
    theaterList.reduce(
      (sum, theater) =>
        sum +
        theater.format_sections.reduce(
          (sectionSum, section) => sectionSum + section.showtimes.length,
          0
        ),
      0
    );

  const extractSharedShowtimesTheaters = () => {
    const shared = [];
    document
      .querySelectorAll(
        "h2.shared-theater-header__name, h3.shared-theater-header__name"
      )
      .forEach((heading) => {
        const name = text(heading);
        if (!name) return;
        const container =
          heading.closest(".shared-showtimes__container") ||
          heading.closest('[class*="shared-showtimes"]');
        if (!container) return;
        const sections = [];
        container
          .querySelectorAll(".shared-showtimes__amenity-group")
          .forEach((group) => {
            const titleEl = group.querySelector(".shared-showtimes__title");
            const amenitiesEl = group.querySelector(".shared-showtimes__amenities");
            const title = text(titleEl);
            const amenities = text(amenitiesEl);
            const sectionLabel =
              [title, amenities].filter(Boolean).join(" · ") || "Standard";
            const showtimes = showtimesFromElements(
              collectShowtimeElements(group)
            );
            if (showtimes.length === 0) return;
            sections.push({
              label: sectionLabel,
              attributes: amenities ? [amenities] : [],
              showtimes,
            });
          });
        if (sections.length === 0) {
          const showtimes = showtimesFromElements(
            collectShowtimeElements(container)
          );
          sections.push({
            label: "Standard",
            attributes: [],
            showtimes,
          });
        }
        shared.push({
          name,
          address: null,
          distance_miles: null,
          format_sections: sections,
        });
      });
    return shared;
  };

  const bodyText = (document.body && document.body.innerText) || "";
  const metaContent = (selector) => {
    const el = document.querySelector(selector);
    const value = el && el.getAttribute ? el.getAttribute("content") : null;
    return value && value.trim() ? value.trim() : null;
  };
  const imageSrc = (img) =>
    img ? img.currentSrc || img.src || img.getAttribute("src") || null : null;
  const posterFromJsonLd = () => {
    const scripts = Array.from(
      document.querySelectorAll('script[type="application/ld+json"]')
    );
    for (const script of scripts) {
      const raw = script.textContent || "";
      if (!raw.trim()) continue;
      try {
        const data = JSON.parse(raw);
        const nodes = Array.isArray(data) ? data : [data];
        for (const node of nodes) {
          const image = node && node.image;
          if (typeof image === "string" && image.trim()) {
            return image.trim();
          }
          if (Array.isArray(image)) {
            const first = image.find((item) => typeof item === "string" && item.trim());
            if (first) return first.trim();
          }
        }
      } catch (_) {
        // Ignore non-JSON script contents; the DOM/image heuristics below still apply.
      }
    }
    return null;
  };
  const posterFromImages = () => {
    const imgs = Array.from(document.querySelectorAll("img"));
    const candidates = imgs
      .map(imageSrc)
      .filter(Boolean)
      .filter((src) => {
        const s = String(src).toLowerCase();
        return (
          !s.includes("default_poster") &&
          (
            /masterrepository\/fandango\/\d+/.test(s) ||
            s.includes("/fandango/") ||
            s.includes("movieposter") ||
            s.includes("poster")
          )
        );
      });
    return candidates[0] || null;
  };

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

      const showtimes = container
        ? showtimesFromElements(collectShowtimeElements(container))
        : [];

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
  // Movie-overview pages render span.showtime-btn (e.g. "7:00p") inside
  // .shared-showtimes__container. Legacy theater-card heuristics can match
  // wrapper nodes with zero parsed times; prefer shared extraction when it
  // finds more showtimes.
  const sharedTheaters = extractSharedShowtimesTheaters();
  if (sharedTheaters.length > 0) {
    const legacyCount = showtimeCountForTheaters(theaters);
    const sharedCount = showtimeCountForTheaters(sharedTheaters);
    if (sharedCount > legacyCount) {
      theaters.length = 0;
      sharedTheaters.forEach((theater) => theaters.push(theater));
    }
  }

  return {
    page_title: document.title || "",
    movie_title:
      text(document.querySelector('h1[class*="movie" i], h1[data-testid*="movie"], h1')) || null,
    release_date_text:
      text(document.querySelector('#movie-detail-release-date, .movie-detail-header__info-item[id="movie-detail-release-date"]')) ||
      null,
    poster_url:
      metaContent('meta[property="og:image"]') ||
      metaContent('meta[name="twitter:image"]') ||
      posterFromJsonLd() ||
      posterFromImages(),
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

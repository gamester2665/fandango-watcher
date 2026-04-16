"""Public API surface for ``fandango_watcher``.

``main`` is the console-script entry point declared in ``pyproject.toml``;
importing it from the package root keeps ``fandango-watcher …`` working.
"""

from __future__ import annotations

from .detect import (
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
    normalize_format_label,
)
from .login import DEFAULT_LOGIN_URL, run_login
from .models import (
    CrawlContext,
    FormatFilter,
    FormatSection,
    FormatTag,
    FullReleasePageData,
    NotOnSalePageData,
    ParsedPageData,
    PartialReleasePageData,
    ReleaseSchema,
    Showtime,
    TheaterListing,
    WatchStatus,
    validate_page_data,
)
from .purchase import (
    InvariantResult,
    PurchaseAttempt,
    PurchaseOutcome,
    PurchasePlan,
    ReviewPageState,
    SeatPick,
    plan_purchase,
    validate_invariant,
)

__all__ = [
    "CrawlContext",
    "DEFAULT_LOGIN_URL",
    "ExtractedFormatSection",
    "ExtractedShowtime",
    "ExtractedTheater",
    "FormatFilter",
    "FormatSection",
    "FormatTag",
    "FullReleasePageData",
    "InvariantResult",
    "NotOnSalePageData",
    "PageSnapshot",
    "ParsedPageData",
    "PartialReleasePageData",
    "PurchaseAttempt",
    "PurchaseOutcome",
    "PurchasePlan",
    "ReleaseSchema",
    "ReviewPageState",
    "SeatPick",
    "Showtime",
    "TheaterListing",
    "WatchStatus",
    "classify",
    "main",
    "normalize_format_label",
    "plan_purchase",
    "run_login",
    "validate_invariant",
    "validate_page_data",
]


def main() -> int:
    """Console-script entry point. Delegates to :mod:`fandango_watcher.cli`."""
    from .cli import main as cli_main

    return cli_main()

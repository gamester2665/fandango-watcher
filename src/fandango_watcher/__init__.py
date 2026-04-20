"""Public API surface for ``fandango_watcher``.

Exports are loaded **lazily** via :func:`__getattr__` so ``import fandango_watcher``,
``pytest`` collection, and ``fandango-watcher --help`` do not eagerly import
Playwright-heavy submodules.

``main`` is defined here because it is the console-script entry point declared
in ``pyproject.toml``.
"""

from __future__ import annotations

import importlib
from typing import Any

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
    "REFERENCE_PAGE_KEYS",
    "REFERENCE_PAGES",
    "REFERENCE_PAGES_READONLY",
    "ReleaseSchema",
    "ReferencePage",
    "ReviewPageState",
    "SeatPick",
    "Showtime",
    "SocialXState",
    "TheaterListing",
    "WatchStatus",
    "XClient",
    "XSignalMatch",
    "check_x_signals",
    "match_tweet",
    "classify",
    "get_reference_page",
    "main",
    "normalize_format_label",
    "plan_purchase",
    "run_login",
    "validate_invariant",
    "validate_page_data",
]

# (module_path, attribute_name) for each public name in ``__all__`` except ``main``.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ExtractedFormatSection": ("fandango_watcher.detect", "ExtractedFormatSection"),
    "ExtractedShowtime": ("fandango_watcher.detect", "ExtractedShowtime"),
    "ExtractedTheater": ("fandango_watcher.detect", "ExtractedTheater"),
    "PageSnapshot": ("fandango_watcher.detect", "PageSnapshot"),
    "classify": ("fandango_watcher.detect", "classify"),
    "normalize_format_label": ("fandango_watcher.detect", "normalize_format_label"),
    "DEFAULT_LOGIN_URL": ("fandango_watcher.login", "DEFAULT_LOGIN_URL"),
    "run_login": ("fandango_watcher.login", "run_login"),
    "CrawlContext": ("fandango_watcher.models", "CrawlContext"),
    "FormatFilter": ("fandango_watcher.models", "FormatFilter"),
    "FormatSection": ("fandango_watcher.models", "FormatSection"),
    "FormatTag": ("fandango_watcher.models", "FormatTag"),
    "FullReleasePageData": ("fandango_watcher.models", "FullReleasePageData"),
    "NotOnSalePageData": ("fandango_watcher.models", "NotOnSalePageData"),
    "ParsedPageData": ("fandango_watcher.models", "ParsedPageData"),
    "PartialReleasePageData": ("fandango_watcher.models", "PartialReleasePageData"),
    "ReleaseSchema": ("fandango_watcher.models", "ReleaseSchema"),
    "Showtime": ("fandango_watcher.models", "Showtime"),
    "TheaterListing": ("fandango_watcher.models", "TheaterListing"),
    "WatchStatus": ("fandango_watcher.models", "WatchStatus"),
    "validate_page_data": ("fandango_watcher.models", "validate_page_data"),
    "InvariantResult": ("fandango_watcher.purchase", "InvariantResult"),
    "PurchaseAttempt": ("fandango_watcher.purchase", "PurchaseAttempt"),
    "PurchaseOutcome": ("fandango_watcher.purchase", "PurchaseOutcome"),
    "PurchasePlan": ("fandango_watcher.purchase", "PurchasePlan"),
    "ReviewPageState": ("fandango_watcher.purchase", "ReviewPageState"),
    "SeatPick": ("fandango_watcher.purchase", "SeatPick"),
    "plan_purchase": ("fandango_watcher.purchase", "plan_purchase"),
    "validate_invariant": ("fandango_watcher.purchase", "validate_invariant"),
    "REFERENCE_PAGE_KEYS": ("fandango_watcher.reference_pages", "REFERENCE_PAGE_KEYS"),
    "REFERENCE_PAGES": ("fandango_watcher.reference_pages", "REFERENCE_PAGES"),
    "REFERENCE_PAGES_READONLY": (
        "fandango_watcher.reference_pages",
        "REFERENCE_PAGES_READONLY",
    ),
    "ReferencePage": ("fandango_watcher.reference_pages", "ReferencePage"),
    "get_reference_page": ("fandango_watcher.reference_pages", "get_reference_page"),
    "SocialXState": ("fandango_watcher.social_x", "SocialXState"),
    "XClient": ("fandango_watcher.social_x", "XClient"),
    "XSignalMatch": ("fandango_watcher.social_x", "XSignalMatch"),
    "check_x_signals": ("fandango_watcher.social_x", "check_x_signals"),
    "match_tweet": ("fandango_watcher.social_x", "match_tweet"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    mod_path, attr = _LAZY_EXPORTS[name]
    module = importlib.import_module(mod_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


def main() -> int:
    """Console-script entry point. Delegates to :mod:`fandango_watcher.cli` package."""
    from .cli import main as cli_main

    return cli_main()

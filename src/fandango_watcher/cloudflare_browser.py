from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from config import BrowserConfig, TargetConfig
from detect import (
    ExtractedFormatSection,
    ExtractedShowtime,
    ExtractedTheater,
    PageSnapshot,
    classify,
)
from models import ParsedPageData

logger = logging.getLogger(__name__)

# Cache for the extractor JS
_EXTRACTOR_JS_CACHE: str | None = None

def _extractor_js() -> str:
    """Load the JS extractor (shared with local Playwright)."""
    global _EXTRACTOR_JS_CACHE
    if _EXTRACTOR_JS_CACHE is None:
        # In the worker, we'll need to make sure this file is bundled or accessible.
        # For now, we'll assume it's in the same directory.
        try:
            import os
            from pathlib import Path
            path = Path(__file__).parent / "extract_page.js"
            _EXTRACTOR_JS_CACHE = path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to load extract_page.js")
            _EXTRACTOR_JS_CACHE = ""
    return _EXTRACTOR_JS_CACHE

async def crawl_target_worker(
    browser_binding: Any,
    target: TargetConfig,
    *,
    citywalk_anchor: str,
) -> ParsedPageData:
    """Crawl a target using Cloudflare Browser Rendering."""
    
    # Cloudflare's browser binding provides a 'fetch' method to interact with a browser instance
    # We use playwright-core or a similar lightweight wrapper if available, 
    # but the raw binding works by sending CDP commands or using their helper library.
    
    # Note: This implementation assumes the 'browser' binding is available in 'env'.
    # We use the 'cloudflare:browser' library which is standard for Workers.
    
    import asyncio
    
    browser = await browser_binding.launch()
    try:
        page = await browser.new_page()
        
        # Navigate
        await page.goto(target.url, wait_until="domcontentloaded", timeout=target.timeout_ms)
        
        # Wait for content
        try:
            await page.wait_for_selector(
                "h2.shared-theater-header__name, [data-testid*='theater-card'], a[href*='ticketing']",
                timeout=15000
            )
        except Exception:
            logger.debug("Browser wait timed out; extracting anyway")

        # Extract
        raw = await page.evaluate(_extractor_js())
        
        # Map to PageSnapshot (simplified mapping for brevity, matching watcher.py)
        theaters = [
            ExtractedTheater(
                name=t["name"],
                format_sections=[
                    ExtractedFormatSection(
                        label=fs["label"],
                        showtimes=[
                            ExtractedShowtime(
                                label=s["label"],
                                ticket_url=s.get("ticket_url"),
                                is_buyable=bool(s.get("is_buyable", True)),
                            )
                            for s in fs.get("showtimes") or []
                        ],
                    )
                    for fs in t.get("format_sections") or []
                ],
            )
            for t in raw.get("theaters") or []
        ]

        snapshot = PageSnapshot(
            url=target.url,
            page_title=raw.get("page_title") or "",
            movie_title=raw.get("movie_title"),
            theaters=theaters,
            ticket_url=raw.get("ticket_url"),
        )
        
        return classify(snapshot, citywalk_anchor=citywalk_anchor)
        
    finally:
        await browser.close()

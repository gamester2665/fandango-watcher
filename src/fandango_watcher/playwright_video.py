"""Finalize Playwright-recorded page videos with stable filenames."""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.sync_api import Page

from .config import BrowserConfig

logger = logging.getLogger(__name__)


def rename_page_video_after_close(
    page: Page,
    *,
    browser_cfg: BrowserConfig,
    label: str,
    stamp: str,
) -> None:
    """Close ``page``, then rename the recorded ``.webm`` to ``{label}-{stamp}.webm``.

    Playwright only exposes :meth:`~playwright.sync_api.Video.path` after the
    page has been closed. Failures are logged at DEBUG and never propagate.
    """
    if not browser_cfg.record_video:
        return
    try:
        page.close()
    except Exception:  # noqa: BLE001
        logger.debug("page.close() skipped or failed during video finalize", exc_info=True)
        return
    try:
        vid = page.video
        if vid is None:
            return
        raw = Path(vid.path())
    except Exception as e:  # noqa: BLE001
        logger.debug("video path unavailable: %s", e)
        return
    if not raw.is_file():
        return
    vdir = Path(browser_cfg.record_video_dir).resolve()
    try:
        vdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug("video dir mkdir failed: %s", e)
        return
    dest = vdir / f"{label}-{stamp}.webm"
    try:
        raw.rename(dest)
    except OSError as e:
        logger.debug("video rename failed %s -> %s: %s", raw, dest, e)

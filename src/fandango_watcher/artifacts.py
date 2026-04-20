"""Prune screenshot, video, trace, and purchase-step artifacts on disk."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import WatcherConfig

logger = logging.getLogger(__name__)


def _prune_file_list(
    paths: list[Path],
    *,
    max_age_days: int | None,
    keep_last_n: int | None,
) -> None:
    existing = [p for p in paths if p.is_file()]
    if not existing:
        return
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    cutoff: float | None = None
    if max_age_days is not None and max_age_days > 0:
        cutoff = now - float(max_age_days) * 86400.0
    for i, p in enumerate(existing):
        try:
            st = p.stat()
        except OSError:
            continue
        too_old = cutoff is not None and st.st_mtime < cutoff
        over_quota = keep_last_n is not None and keep_last_n > 0 and i >= keep_last_n
        if too_old or over_quota:
            try:
                p.unlink()
            except OSError as e:
                logger.debug("prune skip %s: %s", p, e)


def prune_artifact_trees(cfg: WatcherConfig) -> None:
    """Apply ``screenshots.max_age_days`` / ``keep_last_n`` to artifact dirs."""
    s = cfg.screenshots
    max_age = s.max_age_days
    keep = s.keep_last_n

    shot_dir = Path(s.dir)
    if shot_dir.is_dir():
        _prune_file_list(list(shot_dir.glob("*.png")), max_age_days=max_age, keep_last_n=keep)

    vdir = Path(cfg.browser.record_video_dir)
    if vdir.is_dir():
        _prune_file_list(list(vdir.glob("*.webm")), max_age_days=max_age, keep_last_n=keep)

    tdir = Path(cfg.browser.record_trace_dir)
    if tdir.is_dir():
        _prune_file_list(list(tdir.glob("*.zip")), max_age_days=max_age, keep_last_n=keep)

    purchase_root = Path(s.per_purchase_dir)
    if purchase_root.is_dir():
        for sub in purchase_root.iterdir():
            if sub.is_dir():
                _prune_file_list(
                    list(sub.rglob("*.png")),
                    max_age_days=max_age,
                    keep_last_n=None,
                )

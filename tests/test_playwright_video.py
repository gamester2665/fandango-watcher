"""Unit tests for playwright_video.rename_page_video_after_close (mocked Page)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fandango_watcher.config import BrowserConfig, ViewportConfig
from fandango_watcher.playwright_video import rename_page_video_after_close


def _browser_cfg(tmp_path: Path, *, record_video: bool) -> BrowserConfig:
    return BrowserConfig(
        headless=True,
        user_data_dir=str(tmp_path / "profile"),
        viewport=ViewportConfig(),
        record_video=record_video,
        record_video_dir=str(tmp_path / "videos"),
    )


def test_skips_when_record_video_false(tmp_path: Path) -> None:
    cfg = _browser_cfg(tmp_path, record_video=False)
    page = MagicMock()
    rename_page_video_after_close(
        page, browser_cfg=cfg, label="t1", stamp="20260101T000000Z"
    )
    page.close.assert_not_called()


def test_renames_webm_to_label_stamp(tmp_path: Path) -> None:
    vdir = tmp_path / "videos"
    vdir.mkdir(parents=True)
    raw = vdir / "8d048bcd.webm"
    raw.write_bytes(b"webm")

    cfg = _browser_cfg(tmp_path, record_video=True)

    video = MagicMock()
    video.path = MagicMock(return_value=str(raw))
    page = MagicMock()
    page.video = video
    page.close = MagicMock()

    rename_page_video_after_close(
        page, browser_cfg=cfg, label="odyssey-overview", stamp="20260417T034304Z"
    )

    page.close.assert_called_once()
    video.path.assert_called_once()
    dest = vdir / "odyssey-overview-20260417T034304Z.webm"
    assert dest.is_file()
    assert dest.read_bytes() == b"webm"
    assert not raw.exists()


def test_no_video_object_no_crash(tmp_path: Path) -> None:
    cfg = _browser_cfg(tmp_path, record_video=True)
    page = MagicMock()
    page.video = None
    page.close = MagicMock()
    rename_page_video_after_close(
        page, browser_cfg=cfg, label="x", stamp="s"
    )
    page.close.assert_called_once()

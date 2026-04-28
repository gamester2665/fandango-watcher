"""Integration: ``crawl_target`` against a minimal static HTML page over HTTP.

Uses a local ``ThreadingHTTPServer`` (no Fandango network). Exercises the
bundled page extractor + classifier on DOM shaped like a theater card.

The Playwright-backed test is time-bounded via pytest-timeout (``thread``) so a
stuck browser session cannot block ``pytest`` without end.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from fandango_watcher.config import BrowserConfig, TargetConfig, ViewportConfig
from fandango_watcher.models import FormatTag, ReleaseSchema
from fandango_watcher.watcher import crawl_target

# Minimal markup compatible with ``extract_page.js`` theater-card heuristics:
# a heading, a format-ish label, and a ticketing link whose text includes a time.
_STATIC_PAGE = """<!DOCTYPE html>
<html><body>
<p>Get Tickets</p>
<div data-testid="theater-card">
  <h3 class="theater-name">AMC Universal CityWalk 19 + IMAX</h3>
  <div class="format-section">
    <div class="format-header">IMAX 70MM</div>
    <a href="https://www.fandango.com/ticketing/test-showtime">7:00 PM</a>
  </div>
</div>
</body></html>
"""


class _OnePageHandler(BaseHTTPRequestHandler):
    body: ClassVar[bytes] = _STATIC_PAGE.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.mark.integration
@pytest.mark.timeout(300, method="thread")
def test_crawl_target_local_static_theater_card(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OnePageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/movie"

        target = TargetConfig(name="static-fixture", url=url)
        browser_cfg = BrowserConfig(
            headless=True,
            user_data_dir=str(tmp_path / "pw-profile"),
            viewport=ViewportConfig(),
        )
        result = crawl_target(
            target,
            browser_cfg=browser_cfg,
            citywalk_anchor="AMC Universal CityWalk",
            screenshot_dir=None,
            extra_wait_ms=200,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert result.release_schema == ReleaseSchema.PARTIAL_RELEASE
    assert result.citywalk_present is True
    assert result.citywalk_showtime_count >= 1
    assert FormatTag.IMAX_70MM in result.citywalk_formats_seen

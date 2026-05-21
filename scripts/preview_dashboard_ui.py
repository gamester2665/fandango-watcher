#!/usr/bin/env python3
"""Serve a rich dashboard HTML preview for UI review (port 8765)."""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fandango_watcher.dashboard import render_index_html  # noqa: E402

POSTER = "https://picsum.photos/seed/alpha-movie/240/360"
POSTER_BETA = "https://picsum.photos/seed/beta-movie/240/360"
POSTER_GAMMA = "https://picsum.photos/seed/gamma-movie/240/360"
SHOT = "https://picsum.photos/seed/crawl-alpha/960/540"

RICH_SNAP = {
    "healthz": {
        "started_at": "2026-05-20T12:00:00Z",
        "last_tick_at": "2026-05-20T15:58:00Z",
        "last_tick_at_pt": "2026-05-20 08:58:00 PDT",
        "total_ticks": 142,
        "total_errors": 2,
    },
    "targets": [
        {
            "name": "alpha-imax",
            "url": "https://www.fandango.com/alpha-movie",
            "state": {
                "current_state": "watching",
                "last_release_schema": "partial_release",
                "total_ticks": 48,
                "last_success_at": "2026-05-20T15:55:00Z",
            },
            "latest_screenshot_url": SHOT,
            "latest_video_url": "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm",
        },
        {
            "name": "alpha-standard",
            "url": "https://www.fandango.com/alpha-movie-standard",
            "state": {
                "current_state": "error",
                "last_release_schema": "unknown",
                "total_ticks": 12,
                "consecutive_errors": 2,
                "last_error_message": "Session expired during crawl",
            },
            "latest_screenshot_url": SHOT,
        },
        {
            "name": "beta-imax-70mm",
            "url": "https://www.fandango.com/beta-movie",
            "state": {
                "current_state": "alerted",
                "last_release_schema": "full_release",
                "total_ticks": 90,
                "last_success_at": "2026-05-20T15:50:00Z",
            },
            "latest_screenshot_url": SHOT,
        },
        {
            "name": "gamma-standard",
            "url": "https://www.fandango.com/gamma-movie",
            "state": {
                "current_state": "watching",
                "last_release_schema": "showtimes_disclosed",
                "last_showtime_count": 12,
                "last_buyable_showtime_count": 0,
                "total_ticks": 22,
                "last_success_at": "2026-05-20T15:45:00Z",
            },
            "latest_screenshot_url": SHOT,
        },
    ],
    "social_x": {
        "handles": {
            "AlphaFilm": {
                "handle": "AlphaFilm",
                "user_id": "42",
                "last_seen_tweet_id": "123",
                "last_seen_tweet_text": "Tickets for Alpha Movie are on sale now at Fandango!",
                "last_seen_tweet_created_at": "2026-05-20T14:00:00Z",
                "last_polled_at": "2026-05-20T15:00:00Z",
                "last_seen_ticket_analysis": {
                    "announces_tickets": True,
                    "status": "available",
                    "confidence": "high",
                },
            }
        }
    },
    "release_intel": {"status": "disabled", "reason": "preview"},
    "movies": [
        {
            "key": "alpha-movie",
            "title": "Alpha Movie",
            "distributor": "Alpha Distribution",
            "release_date": "2026-07-17",
            "poster_url": POSTER,
            "fandango_targets": ["alpha-imax", "alpha-standard"],
            "x_handles": ["AlphaFilm"],
        },
        {
            "key": "beta-movie",
            "title": "Beta Movie: The Long Title That Should Wrap Cleanly",
            "distributor": "Beta Studios",
            "release_date": "2026-08-01",
            "poster_url": POSTER_BETA,
            "fandango_targets": ["beta-imax-70mm"],
            "x_handles": [],
        },
        {
            "key": "gamma-movie",
            "title": "Gamma Movie",
            "distributor": "Gamma Pictures",
            "release_date": "2026-06-01",
            "poster_url": POSTER_GAMMA,
            "fandango_targets": ["gamma-standard"],
            "x_handles": [],
        },
    ],
    "runtime": {
        "purchase_mode": "notify_only",
        "purchase_enabled": True,
        "fandango_poll": {
            "min_seconds": 30,
            "max_seconds": 35,
            "error_backoff_cap_seconds": 1800,
        },
    },
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            body = render_index_html(RICH_SNAP, refresh_seconds=0).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def main() -> None:
    host, port = "127.0.0.1", 8765
    print(f"Dashboard preview: http://{host}:{port}/")
    HTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

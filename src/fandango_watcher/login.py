"""Headed first-run login flow for Fandango + AMC Stubs.

Opens a Chromium window pointed at Fandango's sign-in page using a
persistent browser context rooted at ``cfg.browser.user_data_dir``. The
human completes login (and verifies AMC Stubs is linked) inside that
window, then presses Enter at the terminal. When the context closes,
Playwright flushes cookies + IndexedDB + localStorage to disk; subsequent
``watch`` / ``once`` runs reuse the same profile via
``crawl_target``'s persistent-context branch.

Tested via ``tests/test_login.py`` with an injected fake Playwright +
fake input function so the suite never launches a real browser.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import BrowserConfig

logger = logging.getLogger(__name__)

# Fandango's stable sign-in URL. Override via ``run_login(login_url=...)``
# if Fandango ever migrates the route.
DEFAULT_LOGIN_URL = "https://www.fandango.com/account/sign-in"


@contextmanager
def _default_playwright_factory() -> Iterator[Any]:
    """Yield a real Playwright context. Injected for tests."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        yield pw


def run_login(
    browser_cfg: BrowserConfig,
    *,
    login_url: str = DEFAULT_LOGIN_URL,
    playwright_factory: Callable[[], _PWContextManager] = _default_playwright_factory,
    wait_input: Callable[[str], str] = input,
    out: Any = sys.stderr,
    headless_override: bool | None = None,
) -> int:
    """Launch a headed Chromium with persistent storage and wait for login.

    The ``browser_cfg`` argument is the same ``BrowserConfig`` from
    ``WatcherConfig``; we honor ``user_data_dir`` for the profile path,
    ``locale`` / ``timezone`` / ``viewport`` for context settings, and
    flip ``headless=False`` by default (override via
    ``headless_override`` if you genuinely want a headless probe).

    Returns 0 on clean shutdown, non-zero if the user cancels (KeyboardInterrupt
    or empty input followed by an explicit "abort").
    """
    profile_path = Path(browser_cfg.user_data_dir)
    profile_path.mkdir(parents=True, exist_ok=True)

    headless = (
        headless_override if headless_override is not None else False
    )

    print(
        "Opening Fandango sign-in. Profile path: "
        f"{profile_path}. Login URL: {login_url}.",
        file=out,
    )
    print(
        "After you complete login AND confirm AMC Stubs is linked, "
        "return here and press Enter to save the profile.",
        file=out,
    )

    with playwright_factory() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_path),
            headless=headless,
            locale=browser_cfg.locale,
            timezone_id=browser_cfg.timezone,
            viewport={
                "width": browser_cfg.viewport.width,
                "height": browser_cfg.viewport.height,
            },
            **browser_cfg.playwright_video_options(),
        )
        try:
            # Reuse an existing tab if Playwright opened one (persistent
            # contexts always come up with at least one page); otherwise
            # create a fresh one.
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, wait_until="domcontentloaded")

            try:
                response = wait_input(
                    "Press Enter when login + AMC Stubs link is complete "
                    "(or type 'abort' to cancel without saving): "
                )
            except (KeyboardInterrupt, EOFError):
                print("login cancelled (interrupt)", file=out)
                return 130

            if response.strip().lower() == "abort":
                print("login cancelled by user", file=out)
                return 1

            print(
                f"login session captured. Profile saved at {profile_path}.",
                file=out,
            )
            return 0
        finally:
            context.close()


# -----------------------------------------------------------------------------
# Type alias for clarity. Playwright's sync_playwright() returns a
# ``PlaywrightContextManager`` from playwright.sync_api but we only need a
# minimal protocol shape for the injection seam, so we don't import the type
# at module load.
# -----------------------------------------------------------------------------
_PWContextManager = Any  # context-manager-of-something-with-.chromium

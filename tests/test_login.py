"""Tests for ``src/fandango_watcher/login.py``.

We never launch a real browser. All Playwright surface is replaced by a
hand-rolled fake so the suite runs in <100 ms with no external deps.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from fandango_watcher.config import BrowserConfig, ViewportConfig
from fandango_watcher.login import DEFAULT_LOGIN_URL, run_login


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))


class _FakeContext:
    def __init__(self, *, with_existing_page: bool = False) -> None:
        self.pages: list[_FakePage] = (
            [_FakePage()] if with_existing_page else []
        )
        self.new_page_calls = 0
        self.closed = False

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(
        self, *, with_existing_page: bool = False
    ) -> None:
        self.last_kwargs: dict[str, Any] = {}
        self.last_user_data_dir: str | None = None
        self.context = _FakeContext(with_existing_page=with_existing_page)

    def launch_persistent_context(
        self, user_data_dir: str, **kwargs: Any
    ) -> _FakeContext:
        self.last_user_data_dir = user_data_dir
        self.last_kwargs = kwargs
        return self.context


class _FakePW:
    def __init__(self, *, with_existing_page: bool = False) -> None:
        self.chromium = _FakeChromium(with_existing_page=with_existing_page)


def _make_factory(
    pw: _FakePW,
):
    @contextmanager
    def factory():
        yield pw

    return factory


def _make_browser_cfg(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(
        headless=True,  # honored only if headless_override is set
        user_data_dir=str(tmp_path / "profile"),
        locale="en-US",
        timezone="America/Los_Angeles",
        viewport=ViewportConfig(width=1440, height=900),
    )


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


class TestRunLoginHappyPath:
    def test_creates_profile_dir(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert rc == 0
        assert (tmp_path / "profile").is_dir()

    def test_navigates_to_default_url(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert rc == 0
        page = pw.chromium.context.pages[-1]
        assert page.goto_calls
        url, kwargs = page.goto_calls[0]
        assert url == DEFAULT_LOGIN_URL
        assert kwargs == {"wait_until": "domcontentloaded"}

    def test_custom_login_url_forwarded(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        rc = run_login(
            cfg,
            login_url="https://example.com/custom-signin",
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert rc == 0
        page = pw.chromium.context.pages[-1]
        assert page.goto_calls[0][0] == "https://example.com/custom-signin"

    def test_browser_kwargs_forwarded(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert pw.chromium.last_user_data_dir == str(tmp_path / "profile")
        kw = pw.chromium.last_kwargs
        assert kw["headless"] is False  # default override: always headed
        assert kw["locale"] == "en-US"
        assert kw["timezone_id"] == "America/Los_Angeles"
        assert kw["viewport"] == {"width": 1440, "height": 900}

    def test_headless_override_forwarded(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
            headless_override=True,
        )
        assert pw.chromium.last_kwargs["headless"] is True

    def test_reuses_existing_page_when_present(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW(with_existing_page=True)
        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert rc == 0
        # Existing page used; no new_page() call needed.
        assert pw.chromium.context.new_page_calls == 0
        assert len(pw.chromium.context.pages) == 1

    def test_context_closed_on_success(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "",
        )
        assert pw.chromium.context.closed is True


class TestRunLoginCancelPaths:
    def test_abort_returns_one(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "abort",
        )
        assert rc == 1
        assert pw.chromium.context.closed is True

    def test_abort_case_insensitive(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()
        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=lambda _prompt: "  ABORT  ",
        )
        assert rc == 1

    def test_keyboard_interrupt_returns_130(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()

        def raise_interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt

        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=raise_interrupt,
        )
        assert rc == 130
        assert pw.chromium.context.closed is True

    def test_eof_treated_like_interrupt(self, tmp_path: Path) -> None:
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()

        def raise_eof(_prompt: str) -> str:
            raise EOFError

        rc = run_login(
            cfg,
            playwright_factory=_make_factory(pw),
            wait_input=raise_eof,
        )
        assert rc == 130
        assert pw.chromium.context.closed is True

    def test_context_closed_even_if_goto_raises(self, tmp_path: Path) -> None:
        """The finally block must run even if Playwright explodes mid-goto."""
        cfg = _make_browser_cfg(tmp_path)
        pw = _FakePW()

        def boom(self_page: _FakePage, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("network down")

        # Patch the FakePage class so the freshly-created page raises.
        original = _FakePage.goto
        _FakePage.goto = boom  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="network down"):
                run_login(
                    cfg,
                    playwright_factory=_make_factory(pw),
                    wait_input=lambda _prompt: "",
                )
        finally:
            _FakePage.goto = original  # type: ignore[assignment]

        assert pw.chromium.context.closed is True

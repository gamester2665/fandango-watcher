"""Tests for src/fandango_watcher/cli.py.

Covers:

* ``build_parser`` exposes all expected subcommands with their flags
* ``once --url`` is routed correctly (we don't actually launch Playwright;
  the watcher function is monkeypatched to intercept the call)
* ``once`` without a config file reports a clear error
* ``test-purchase --from-fixture`` runs the planner end-to-end without
  touching Playwright
* ``login`` forwards the resolved browser config + URL to ``run_login``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fandango_watcher import cli
from fandango_watcher.models import (
    NotOnSalePageData,
)


# -----------------------------------------------------------------------------
# Parser shape
# -----------------------------------------------------------------------------


class TestBuildParser:
    def test_subcommand_is_required(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_all_expected_subcommands_exist(self) -> None:
        parser = cli.build_parser()
        # argparse stores subparsers under a single action; find it.
        subparsers_action = next(
            a
            for a in parser._actions  # noqa: SLF001 — test-only introspection
            if a.__class__.__name__ == "_SubParsersAction"
        )
        names = set(subparsers_action.choices)
        assert names == {"once", "watch", "login", "test-notify", "test-purchase"}

    def test_once_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "once",
                "--config",
                "foo.yaml",
                "--target",
                "t1",
                "--url",
                "https://x",
                "--no-screenshot",
                "--dry-run",
                "--headed",
            ]
        )
        assert ns.command == "once"
        assert ns.config == "foo.yaml"
        assert ns.target == "t1"
        assert ns.url == "https://x"
        assert ns.no_screenshot is True
        assert ns.dry_run is True
        assert ns.headed is True

    def test_log_level_choices_enforced(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--log-level", "CHATTY", "watch"])


# -----------------------------------------------------------------------------
# New subcommand parsers (shape only)
# -----------------------------------------------------------------------------


class TestLoginParser:
    def test_login_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args([
            "login",
            "--config",
            "cfg.yaml",
            "--login-url",
            "https://example.com/signin",
            "--headless",
        ])
        assert ns.command == "login"
        assert ns.config == "cfg.yaml"
        assert ns.login_url == "https://example.com/signin"
        assert ns.headless is True

    def test_login_defaults(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(["login"])
        assert ns.command == "login"
        assert ns.config is None
        assert ns.login_url is None
        assert ns.headless is False


class TestTestPurchaseParser:
    def test_test_purchase_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args([
            "test-purchase",
            "--config",
            "cfg.yaml",
            "--target",
            "t1",
            "--from-fixture",
            "fixtures/foo.json",
            "--no-screenshot",
        ])
        assert ns.command == "test-purchase"
        assert ns.config == "cfg.yaml"
        assert ns.target == "t1"
        assert ns.from_fixture == "fixtures/foo.json"
        assert ns.no_screenshot is True


# -----------------------------------------------------------------------------
# `once` routing
# -----------------------------------------------------------------------------


def _make_stub_result() -> NotOnSalePageData:
    return NotOnSalePageData(
        url="https://fandango.com/adhoc",
        page_title="Stub",
        theater_count=0,
        showtime_count=0,
    )


class TestOnceAdHocUrl:
    def test_ad_hoc_url_skips_config_and_calls_watcher(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            captured["target_name"] = target.name
            captured["target_url"] = target.url
            captured["headless"] = browser_cfg.headless
            captured["citywalk_anchor"] = citywalk_anchor
            captured["screenshot_dir"] = screenshot_dir
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "once",
                "--url",
                "https://www.fandango.com/x",
                "--no-screenshot",
                "--headed",
            ]
        )
        assert rc == 0
        assert captured["target_name"] == "adhoc"
        assert captured["target_url"] == "https://www.fandango.com/x"
        assert captured["headless"] is False  # --headed flips this off
        assert captured["screenshot_dir"] is None

        out = capsys.readouterr().out
        assert '"release_schema": "not_on_sale"' in out


class TestOnceConfigMissing:
    def test_missing_config_file_reports_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)

        rc = cli.main(["once"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "config file not found" in err


class TestOnceViaExampleConfig:
    """End-to-end `once` using config.example.yaml, with Playwright stubbed.

    Verifies that:

    * the CLI resolves a real config file
    * the correct target is picked (--target)
    * browser + theater settings are forwarded to the watcher
    """

    def test_target_name_selects_correct_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        captured: dict[str, Any] = {}

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            captured["target_name"] = target.name
            captured["target_url"] = target.url
            captured["citywalk_anchor"] = citywalk_anchor
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "once",
                "--config",
                str(config_path),
                "--target",
                "odyssey-overview",
                "--no-screenshot",
            ]
        )
        assert rc == 0
        assert captured["target_name"] == "odyssey-overview"
        assert "CityWalk" in captured["citywalk_anchor"]

    def test_unknown_target_name_reports_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        rc = cli.main(
            [
                "once",
                "--config",
                str(config_path),
                "--target",
                "not-a-real-target",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "no target named" in err
        assert "odyssey-imax-70mm" in err  # valid option listed for the user


# -----------------------------------------------------------------------------
# `login`
# -----------------------------------------------------------------------------


class TestLoginCommand:
    def test_login_forwards_browser_cfg_and_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        captured: dict[str, Any] = {}

        def fake_run_login(
            browser_cfg, *, login_url, headless_override=None
        ):  # type: ignore[no-untyped-def]
            captured["user_data_dir"] = browser_cfg.user_data_dir
            captured["login_url"] = login_url
            captured["headless_override"] = headless_override
            return 0

        monkeypatch.setattr("fandango_watcher.login.run_login", fake_run_login)

        rc = cli.main([
            "login",
            "--config",
            str(config_path),
            "--login-url",
            "https://example.com/signin",
        ])
        assert rc == 0
        assert captured["login_url"] == "https://example.com/signin"
        assert captured["headless_override"] is None
        assert captured["user_data_dir"]  # something was loaded from config

    def test_login_headless_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)

        captured: dict[str, Any] = {}

        def fake_run_login(
            browser_cfg, *, login_url, headless_override=None
        ):  # type: ignore[no-untyped-def]
            captured["headless_override"] = headless_override
            captured["login_url"] = login_url
            return 0

        monkeypatch.setattr("fandango_watcher.login.run_login", fake_run_login)

        rc = cli.main(["login", "--headless"])
        assert rc == 0
        assert captured["headless_override"] is True
        # default URL when --login-url is not passed
        assert "fandango.com" in captured["login_url"]


# -----------------------------------------------------------------------------
# `test-purchase`
# -----------------------------------------------------------------------------


class TestTestPurchaseFromFixture:
    def test_from_fixture_runs_planner_without_playwright(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        # Build a minimal partial-release fixture with a CityWalk IMAX_70MM
        # showtime that matches the example config's seat_priority.
        fixture = {
            "release_schema": "partial_release",
            "watch_status": "watchable",
            "url": "https://www.fandango.com/movie/odyssey",
            "page_title": "The Odyssey Tickets",
            "movie_title": "The Odyssey",
            "crawled_at": "2026-12-25T18:00:00+00:00",
            "schema_evidence": ["fixture"],
            "theater_count": 1,
            "showtime_count": 1,
            "formats_seen": ["IMAX_70MM"],
            "citywalk_present": True,
            "citywalk_showtime_count": 1,
            "citywalk_formats_seen": ["IMAX_70MM"],
            "theaters": [
                {
                    "name": "AMC Universal CityWalk 19",
                    "is_citywalk": True,
                    "format_sections": [
                        {
                            "label": "IMAX 70MM",
                            "normalized_format": "IMAX_70MM",
                            "attributes": [],
                            "showtimes": [
                                {
                                    "label": "7:00p",
                                    "ticket_url": "https://www.fandango.com/buy/x",
                                    "is_buyable": True,
                                    "is_citywalk": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        rc = cli.main([
            "test-purchase",
            "--config",
            str(config_path),
            "--from-fixture",
            str(fixture_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["release_schema"] == "partial_release"
        plan = payload["plan"]
        assert plan is not None
        assert plan["theater_name"] == "AMC Universal CityWalk 19"
        assert plan["showtime_label"] == "7:00p"
        assert plan["format_tag"] == "IMAX_70MM"
        # config.example.yaml puts row N in auditorium 19 for IMAX_70MM
        assert plan["auditorium"] == 19

    def test_from_fixture_no_plan_when_not_on_sale(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        fixture = {
            "release_schema": "not_on_sale",
            "watch_status": "not_watchable",
            "url": "https://www.fandango.com/movie/odyssey",
            "page_title": "The Odyssey - Coming Soon",
            "crawled_at": "2026-12-25T18:00:00+00:00",
            "schema_evidence": ["fixture"],
            "theater_count": 0,
            "showtime_count": 0,
            "formats_seen": [],
            "citywalk_present": False,
            "citywalk_showtime_count": 0,
            "citywalk_formats_seen": [],
            "theaters": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        rc = cli.main([
            "test-purchase",
            "--config",
            str(config_path),
            "--from-fixture",
            str(fixture_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["plan"] is None
        assert payload["release_schema"] == "not_on_sale"
        assert "no plan" in payload["reason"]

    def test_missing_fixture_file_reports_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        rc = cli.main([
            "test-purchase",
            "--config",
            str(config_path),
            "--from-fixture",
            str(tmp_path / "nope.json"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "fixture file not found" in err

    def test_missing_config_reports_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)
        rc = cli.main(["test-purchase"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "config file not found" in err

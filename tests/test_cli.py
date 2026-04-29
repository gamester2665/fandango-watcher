# mypy: disable-error-code="arg-type,unused-ignore,dict-item"
"""Tests for ``fandango_watcher.cli`` (package under ``src/fandango_watcher/cli/``).

Covers:

* ``build_parser`` exposes all expected subcommands with their flags
* ``once --url`` is routed correctly (we don't actually launch Playwright;
  the watcher function is monkeypatched to intercept the call)
* ``once`` without a config file reports a clear error
* ``once --write-state`` persists ``state/<target>.json`` (config mode only)
* ``test-purchase --from-fixture`` runs the planner end-to-end without
  touching Playwright
* ``login`` forwards the resolved browser config + URL to ``run_login``
* ``refs`` prints bundled reference URLs without touching the network
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


class TestResolveConfigPath:
    def test_fallback_to_config_example_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fandango_watcher.cli.commands import _resolve_config_path

        repo = Path(__file__).resolve().parent.parent
        (tmp_path / "config.example.yaml").write_text(
            (repo / "config.example.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)
        got = _resolve_config_path(None)
        assert got.name == "config.example.yaml"
        assert got.is_file()

    def test_no_fallback_in_empty_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fandango_watcher.cli.commands import _resolve_config_path

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)
        got = _resolve_config_path(None)
        assert got.name == "config.yaml"
        assert not got.is_file()


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
        assert names == {
            "once",
            "watch",
            "dashboard",
            "api-drift",
            "login",
            "test-notify",
            "test-purchase",
            "refs",
            "x-poll",
            "movies",
            "dump-review",
            "doctor",
        }

    def test_api_drift_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "api-drift",
                "--config",
                "foo.yaml",
                "--max-dates",
                "3",
                "--output",
                "json",
            ]
        )
        assert ns.command == "api-drift"
        assert ns.config == "foo.yaml"
        assert ns.max_dates == 3
        assert ns.output == "json"


class TestApiDriftCommand:
    def test_api_drift_prints_json_with_mocked_client(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            """
targets:
  - name: t1
    url: https://example.com/m
theater:
  display_name: CW
  fandango_theater_anchor: AMC Universal CityWalk
formats:
  require: []
  include: []
direct_api:
  theater_id: AAAWX
poll:
  min_seconds: 30
  max_seconds: 30
purchase:
  enabled: false
  mode: notify_only
notify:
  channels: []
  on_events: []
""",
            encoding="utf-8",
        )

        class FakeClient:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, *_exc_info: object) -> None:
                pass

        def fake_drift_check(_client: FakeClient, *, max_dates: int) -> dict[str, Any]:
            return {
                "ok": True,
                "inspected_dates": ["2026-04-28"][:max_dates],
                "format_names_seen": ["IMAX"],
                "calendar_date_count": 1,
            }

        monkeypatch.setattr("fandango_watcher.fandango_api.FandangoApiClient", FakeClient)
        monkeypatch.setattr("fandango_watcher.fandango_api.drift_check", fake_drift_check)

        rc = cli.main(
            [
                "api-drift",
                "--config",
                str(cfg_path),
                "--max-dates",
                "1",
                "--output",
                "json",
            ]
        )

        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["format_names_seen"] == ["IMAX"]

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
                "--write-state",
                "--direct-api-mode",
                "browser",
                "--no-browser-fallback",
                "--format-filter-selector",
                "#lazyload-format-filters li",
                "--format-filter-label",
                "IMAX 3D",
                "--format-filter-timeout-ms",
                "9000",
            ]
        )
        assert ns.command == "once"
        assert ns.config == "foo.yaml"
        assert ns.target == "t1"
        assert ns.url == "https://x"
        assert ns.no_screenshot is True
        assert ns.dry_run is True
        assert ns.headed is True
        assert ns.write_state is True
        assert ns.direct_api_mode == "browser"
        assert ns.no_browser_fallback is True
        assert ns.format_filter_selector == "#lazyload-format-filters li"
        assert ns.format_filter_label == "IMAX 3D"
        assert ns.format_filter_timeout_ms == 9000

    def test_watch_accepts_direct_api_switching_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "watch",
                "--config",
                "foo.yaml",
                "--direct-api-mode",
                "api",
                "--no-browser-fallback",
            ]
        )
        assert ns.command == "watch"
        assert ns.direct_api_mode == "api"
        assert ns.no_browser_fallback is True

    def test_log_level_choices_enforced(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--log-level", "CHATTY", "watch"])


class TestDoctor:
    def test_missing_config_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)
        rc = cli.main(["doctor", "--config", "nope.yaml"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "config not found" in err.lower()

    def test_invalid_yaml_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("targets: [", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["doctor", "--config", str(bad)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid config" in err.lower()

    def test_json_mode_on_example_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = Path(__file__).resolve().parent.parent
        (tmp_path / "config.yaml").write_text(
            (repo / "config.example.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WATCHER_CONFIG", raising=False)
        rc = cli.main(["doctor", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert data["purchase_mode"] == "full_auto"
        assert any("full_auto" in w for w in data["warnings"])
        assert isinstance(data["notify"]["active_channels"], list)


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


class TestDashboardParser:
    def test_dashboard_accepts_host_port_no_open(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "dashboard",
                "--config",
                "cfg.yaml",
                "--host",
                "0.0.0.0",
                "--port",
                "9999",
                "--no-open",
            ]
        )
        assert ns.command == "dashboard"
        assert ns.config == "cfg.yaml"
        assert ns.host == "0.0.0.0"
        assert ns.port == 9999
        assert ns.no_open is True


class TestWatchNoOpen:
    def test_watch_parses_no_open(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(["watch", "--no-open", "--no-healthz"])
        assert ns.command == "watch"
        assert ns.no_open is True
        assert ns.no_healthz is True

    def test_watch_accepts_format_filters(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "watch",
                "--no-healthz",
                "--format-filter-label",
                "IMAX 3D",
                "--format-filter-timeout-ms",
                "15000",
            ]
        )
        assert ns.format_filter_label == "IMAX 3D"
        assert ns.format_filter_timeout_ms == 15000

    def test_watch_browser_mode_disables_direct_api(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            """
targets:
  - name: t1
    url: https://example.com/m
theater:
  display_name: CW
  fandango_theater_anchor: AMC Universal CityWalk
formats:
  require: []
  include: []
direct_api:
  enabled: true
  fallback_to_browser: true
poll:
  min_seconds: 30
  max_seconds: 30
purchase:
  enabled: false
  mode: notify_only
notify:
  channels: []
  on_events: []
""",
            encoding="utf-8",
        )
        captured: dict[str, Any] = {}

        def fake_run_watch(cfg, *_args: Any, **_kwargs: Any) -> int:  # type: ignore[no-untyped-def]
            captured["direct_api_enabled"] = cfg.direct_api.enabled
            captured["fallback_to_browser"] = cfg.direct_api.fallback_to_browser
            return 0

        monkeypatch.setattr("fandango_watcher.loop.run_watch", fake_run_watch)

        rc = cli.main(
            [
                "watch",
                "--config",
                str(cfg_path),
                "--no-healthz",
                "--direct-api-mode",
                "browser",
                "--no-browser-fallback",
            ]
        )

        assert rc == 0
        assert captured == {
            "direct_api_enabled": False,
            "fallback_to_browser": False,
        }


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
            "--format-filter-label",
            "IMAX 3D",
            "--stub",
        ])
        assert ns.command == "test-purchase"
        assert ns.config == "cfg.yaml"
        assert ns.target == "t1"
        assert ns.from_fixture == "fixtures/foo.json"
        assert ns.no_screenshot is True
        assert ns.format_filter_label == "IMAX 3D"
        assert ns.stub is True


class TestDumpReviewParser:
    def test_dump_review_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "dump-review",
                "--url",
                "https://www.fandango.com/checkout/foo",
                "--name",
                "odyssey_imax_70mm_2026",
                "--out-dir",
                "tests/fixtures/review_pages",
                "--wait-ms",
                "5000",
                "--headed",
            ],
        )
        assert ns.command == "dump-review"
        assert ns.url == "https://www.fandango.com/checkout/foo"
        assert ns.name == "odyssey_imax_70mm_2026"
        assert ns.out_dir == "tests/fixtures/review_pages"
        assert ns.wait_ms == 5000
        assert ns.headed is True

    def test_dump_review_requires_url_and_name(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["dump-review"])


class TestXPollCheckBearerFlag:
    def test_check_bearer_flag_parses(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(["x-poll", "--check-bearer"])
        assert ns.command == "x-poll"
        assert ns.check_bearer is True

    def test_check_bearer_defaults_false(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(["x-poll"])
        assert ns.check_bearer is False


class TestRefsParser:
    def test_refs_accepts_expected_flags(self) -> None:
        parser = cli.build_parser()
        ns = parser.parse_args(
            ["refs", "--key", "project_hail_mary", "--output", "table"],
        )
        assert ns.command == "refs"
        assert ns.key == "project_hail_mary"
        assert ns.output == "table"


class TestRefsCommand:
    def test_refs_json_lists_all_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["refs"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        keys = {d["key"] for d in data}
        assert keys == {
            "the_odyssey_imax_70mm",
            "dune_part_three_imax_70mm",
            "the_mandalorian_and_grogu",
            "project_hail_mary",
        }

    def test_refs_single_key_emits_one_object(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["refs", "--key", "the_odyssey_imax_70mm"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["key"] == "the_odyssey_imax_70mm"
        assert "movie-overview" in data["url"]

    def test_refs_unknown_key_errors(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(["refs", "--key", "nope"])
        assert rc == 1
        assert "unknown reference key" in capsys.readouterr().err


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
            captured["format_label"] = target.format_filter_click_label
            captured["format_selector"] = target.format_filter_click_selector
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "once",
                "--url",
                "https://www.fandango.com/x",
                "--no-screenshot",
                "--headed",
                "--format-filter-label",
                "IMAX 3D",
            ]
        )
        assert rc == 0
        assert captured["target_name"] == "adhoc"
        assert captured["target_url"] == "https://www.fandango.com/x"
        assert captured["headless"] is False  # --headed flips this off
        assert captured["screenshot_dir"] is None
        assert captured["format_label"] == "IMAX 3D"
        assert captured["format_selector"] is None

        out = capsys.readouterr().out
        assert '"release_schema": "not_on_sale"' in out

    def test_video_and_trace_flags_set_browser_cfg(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            captured["record_video"] = browser_cfg.record_video
            captured["record_trace"] = browser_cfg.record_trace
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "once",
                "--url",
                "https://www.fandango.com/x",
                "--no-screenshot",
                "--video",
                "--trace",
            ]
        )
        assert rc == 0
        assert captured["record_video"] is True
        assert captured["record_trace"] is True
        capsys.readouterr()  # drain stdout


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

        def fake_direct(target, cfg):  # type: ignore[no-untyped-def]
            from types import SimpleNamespace

            from fandango_watcher.direct_api_detect import DirectApiDetectionMeta

            captured["target_name"] = target.name
            captured["target_url"] = target.url
            captured["citywalk_anchor"] = cfg.theater.fandango_theater_anchor
            return SimpleNamespace(
                parsed=_make_stub_result(),
                meta=DirectApiDetectionMeta(inspected_dates=["2026-04-28"]),
            )

        monkeypatch.setattr("fandango_watcher.direct_api_detect.detect_target_direct_api", fake_direct)

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

    def test_format_filter_cli_overrides_config_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

        captured: dict[str, Any] = {}

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            captured["label"] = target.format_filter_click_label
            captured["selector"] = target.format_filter_click_selector
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
                "--format-filter-label",
                "IMAX 3D",
            ]
        )
        assert rc == 0
        assert captured["label"] == "IMAX 3D"
        assert captured["selector"] is None
        capsys.readouterr()

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

    def test_write_state_with_url_ad_hoc_errors(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli.main(
            [
                "once",
                "--url",
                "https://www.fandango.com/x",
                "--write-state",
                "--no-screenshot",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--write-state" in err
        assert "--config" in err

    def test_write_state_persists_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state_dir = tmp_path / "state"
        cfg_text = f"""
targets:
  - name: t1
    url: https://example.com/m
theater:
  display_name: CW
  fandango_theater_anchor: AMC Universal CityWalk
formats:
  require: [IMAX]
  include: []
poll:
  min_seconds: 30
  max_seconds: 30
  error_backoff_multiplier: 2
  error_backoff_cap_seconds: 1800
purchase:
  enabled: false
  mode: notify_only
notify:
  channels: []
  on_events: []
screenshots:
  dir: {repr(str(tmp_path / "artifacts" / "screenshots"))}
  per_purchase_dir: {repr(str(tmp_path / "artifacts" / "purchase"))}
state:
  dir: {repr(str(state_dir))}
browser:
  headless: true
  user_data_dir: {repr(str(tmp_path / "profile"))}
direct_api:
  enabled: false
"""
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(cfg_text, encoding="utf-8")

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "once",
                "--config",
                str(cfg_path),
                "--target",
                "t1",
                "--no-screenshot",
                "--write-state",
            ]
        )
        assert rc == 0
        assert (state_dir / "t1.json").is_file()
        out = json.loads(capsys.readouterr().out)
        assert "parsed" in out and "state_write" in out
        assert out["parsed"]["release_schema"] == "not_on_sale"
        assert out["state_write"]["path"].endswith("t1.json")
        assert "target_state" in out["state_write"]


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
    def test_stub_blocked_when_full_auto_without_allow_flag(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"
        rc = cli.main(
            ["test-purchase", "--config", str(config_path), "--stub"]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "full_auto" in err
        assert "--allow-stub-with-full-auto" in err

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

    def test_stub_errors_when_no_plan(
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
            "--stub",
            "--allow-stub-with-full-auto",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--stub requires a purchase plan" in err

    def test_stub_invokes_run_scripted_purchase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"

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

        captured: dict[str, Any] = {}

        def fake_run_scripted(*args: Any, **kwargs: Any) -> object:
            captured["hold_for_confirm"] = kwargs.get("hold_for_confirm")
            class _A:
                def model_dump(self, mode: str = "json") -> dict[str, str]:
                    return {"outcome": "held_for_confirm", "stub": True}

            return _A()

        monkeypatch.setattr(
            "fandango_watcher.purchaser.run_scripted_purchase",
            fake_run_scripted,
        )

        rc = cli.main([
            "test-purchase",
            "--config",
            str(config_path),
            "--from-fixture",
            str(fixture_path),
            "--stub",
            "--allow-stub-with-full-auto",
        ])
        assert rc == 0
        assert captured.get("hold_for_confirm") is True
        out = json.loads(capsys.readouterr().out)
        assert out["purchase_attempt"]["outcome"] == "held_for_confirm"

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

    def test_live_crawl_passes_format_filter_to_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config.example.yaml"
        captured: dict[str, Any] = {}

        def fake_crawl(target, *, browser_cfg, citywalk_anchor, screenshot_dir):  # type: ignore[no-untyped-def]
            captured["label"] = target.format_filter_click_label
            return _make_stub_result()

        monkeypatch.setattr("fandango_watcher.watcher.crawl_target", fake_crawl)

        rc = cli.main(
            [
                "test-purchase",
                "--config",
                str(config_path),
                "--target",
                "odyssey-overview",
                "--no-screenshot",
                "--format-filter-label",
                "IMAX 3D",
            ]
        )
        assert rc == 0
        assert captured["label"] == "IMAX 3D"
        out = capsys.readouterr().out
        assert '"plan": null' in out or '"plan": null,' in out

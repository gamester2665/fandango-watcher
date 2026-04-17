"""Tests for .env.example.

Verifies that every secret referenced by PLAN.md is documented, and that the
leftover xAI/Grok keys from the scaffolding seed are no longer present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"


@pytest.fixture(scope="module")
def env_keys() -> set[str]:
    raw = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        keys.add(key.strip())
    return keys


class TestRuntimeKeys:
    def test_tz_configured(self, env_keys: set[str]) -> None:
        assert "TZ" in env_keys

    def test_watcher_mode_and_config_path(self, env_keys: set[str]) -> None:
        assert "WATCHER_MODE" in env_keys
        assert "WATCHER_CONFIG" in env_keys


class TestTwilioKeys:
    def test_all_twilio_keys_present(self, env_keys: set[str]) -> None:
        required = {
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM",
            "NOTIFY_TO_E164",
        }
        assert required.issubset(env_keys), f"missing: {required - env_keys}"


class TestSmtpKeys:
    def test_all_smtp_keys_present(self, env_keys: set[str]) -> None:
        required = {
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "SMTP_FROM",
            "NOTIFY_TO_EMAIL",
        }
        assert required.issubset(env_keys), f"missing: {required - env_keys}"


class TestAgentFallbackKey:
    def test_openai_and_openrouter_keys_documented(self, env_keys: set[str]) -> None:
        assert "OPENAI_API_KEY" in env_keys
        assert "OPENROUTER_API_KEY" in env_keys

    def test_no_anthropic_key(self, env_keys: set[str]) -> None:
        # Anthropic Computer Use was dropped in favor of the OSS browser-use
        # provider; the legacy key must not reappear.
        assert "ANTHROPIC_API_KEY" not in env_keys


class TestLegacyCruftRemoved:
    def test_no_xai_grok_keys(self, env_keys: set[str]) -> None:
        # XAI_* (xAI / Grok) was a leftover from the scaffolding seed and
        # is now decisively unused. The X_* keys (Twitter Developer API)
        # ARE expected and tested in :class:`TestSocialXKeys` below.
        forbidden = {"XAI_API_KEY", "XAI_API_SECRET", "XAI_API_BEARER_TOKEN"}
        leaked = forbidden & env_keys
        assert not leaked, f"leftover xAI keys in .env.example: {leaked}"


class TestSocialXKeys:
    """Phase 2.5 — X / Twitter Developer API credentials for social signals."""

    def test_bearer_and_app_keys_present(self, env_keys: set[str]) -> None:
        required = {"X_API_KEY", "X_API_KEY_SECRET", "X_BEARER_TOKEN"}
        assert required.issubset(env_keys), f"missing: {required - env_keys}"


class TestNoSecretValuesLeaked:
    """A .env.example file should ship with placeholder values only."""

    def test_twilio_values_are_placeholders(self) -> None:
        raw = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped.startswith("TWILIO_ACCOUNT_SID="):
                continue
            _, _, value = stripped.partition("=")
            value = value.strip()
            # Real Twilio SIDs start with "AC" and are 34 chars. A placeholder
            # should be empty or an obviously fake token.
            assert not (value.startswith("AC") and len(value) >= 30), (
                "TWILIO_ACCOUNT_SID in .env.example looks like a real credential"
            )

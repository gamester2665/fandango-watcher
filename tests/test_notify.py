"""Tests for src/fandango_watcher/notify.py.

Covers:

* ``FanOutNotifier`` collects per-channel outcomes without short-circuiting
* ``build_notifier`` drops channels with missing creds
* ``TwilioNotifier`` truncates oversized SMS bodies and forwards to the client
* ``SmtpNotifier`` picks the right transport based on port (465/587/other)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fandango_watcher.config import NotifyConfig, Settings
from fandango_watcher.notify import (
    ChannelResult,
    FanOutNotifier,
    NotificationMessage,
    Notifier,
    SmtpNotifier,
    TwilioNotifier,
    build_notifier,
)


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class _CapturingNotifier(Notifier):
    def __init__(self, name: str, *, fail_with: BaseException | None = None) -> None:
        self._name = name
        self._fail_with = fail_with
        self.sent: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, msg: NotificationMessage) -> None:
        self.sent.append(msg)
        if self._fail_with is not None:
            raise self._fail_with


class _FakeTwilioMessages:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(self, *, body: str, from_: str, to: str) -> None:
        self.created.append({"body": body, "from_": from_, "to": to})


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = _FakeTwilioMessages()


class _FakeSmtpSession:
    """Records every operation invoked on it so tests can assert call order."""

    def __init__(self, calls_log: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._log = calls_log

    def __enter__(self) -> "_FakeSmtpSession":
        self._log.append(("__enter__", ()))
        return self

    def __exit__(self, *exc: object) -> None:
        self._log.append(("__exit__", ()))

    def starttls(self) -> None:
        self._log.append(("starttls", ()))

    def login(self, user: str, password: str) -> None:
        self._log.append(("login", (user, password)))

    def send_message(self, em: object) -> None:
        self._log.append(("send_message", (em,)))


def _make_fake_smtp_cls(
    log: list[tuple[str, tuple[Any, ...]]],
) -> type:
    class _FakeSmtpClass:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            log.append(("init", (host, port, timeout)))

        def __enter__(self) -> _FakeSmtpSession:
            return _FakeSmtpSession(log).__enter__()

        def __exit__(self, *exc: object) -> None:
            _FakeSmtpSession(log).__exit__(*exc)

    return _FakeSmtpClass


# -----------------------------------------------------------------------------
# FanOutNotifier
# -----------------------------------------------------------------------------


class TestFanOutNotifier:
    def test_sends_to_every_channel(self) -> None:
        a = _CapturingNotifier("a")
        b = _CapturingNotifier("b")
        fan = FanOutNotifier([a, b])
        msg = NotificationMessage(event="e", subject="s", body="body")

        results = fan.send(msg)

        assert [r.name for r in results] == ["a", "b"]
        assert all(r.ok for r in results)
        assert a.sent == [msg] and b.sent == [msg]

    def test_channel_failure_does_not_short_circuit(self) -> None:
        a = _CapturingNotifier("a", fail_with=RuntimeError("boom"))
        b = _CapturingNotifier("b")
        fan = FanOutNotifier([a, b])

        results = fan.send(NotificationMessage(event="e", subject="s", body="b"))

        by_name = {r.name: r for r in results}
        assert by_name["a"].ok is False
        assert isinstance(by_name["a"].error, RuntimeError)
        assert by_name["b"].ok is True
        assert b.sent  # b still received the message

    def test_empty_notifier_list_is_no_op(self) -> None:
        fan = FanOutNotifier([])
        assert fan.channel_names == []
        assert fan.send(NotificationMessage(event="e", subject="s", body="b")) == []


# -----------------------------------------------------------------------------
# TwilioNotifier
# -----------------------------------------------------------------------------


class TestTwilioNotifier:
    def test_forwards_to_injected_client(self) -> None:
        fake = _FakeTwilioClient()
        t = TwilioNotifier(
            account_sid="sid",
            auth_token="tok",
            from_number="+15551234567",
            to_number="+15559876543",
            client=fake,
        )
        t.send(NotificationMessage(event="ev", subject="subj", body="body"))

        assert len(fake.messages.created) == 1
        call = fake.messages.created[0]
        assert "[ev]" in call["body"]
        assert "subj" in call["body"]
        assert "body" in call["body"]
        assert call["from_"] == "+15551234567"
        assert call["to"] == "+15559876543"

    def test_long_body_is_truncated(self) -> None:
        fake = _FakeTwilioClient()
        t = TwilioNotifier(
            account_sid="sid",
            auth_token="tok",
            from_number="+1",
            to_number="+2",
            client=fake,
        )
        huge = "x" * 5000
        t.send(NotificationMessage(event="ev", subject="s", body=huge))

        sent_body = fake.messages.created[0]["body"]
        assert len(sent_body) <= 1400
        assert sent_body.endswith("...")

    def test_ignores_email_attachments_on_sms(self, tmp_path: Path) -> None:
        fake = _FakeTwilioClient()
        t = TwilioNotifier(
            account_sid="sid",
            auth_token="tok",
            from_number="+1",
            to_number="+2",
            client=fake,
        )
        p = tmp_path / "nope.png"
        p.write_bytes(b"hi")
        t.send(
            NotificationMessage(
                event="ev",
                subject="s",
                body="b",
                email_attachments=[("nope.png", p)],
            )
        )
        assert len(fake.messages.created) == 1


# -----------------------------------------------------------------------------
# SmtpNotifier
# -----------------------------------------------------------------------------


class TestSmtpNotifier:
    def test_port_465_uses_implicit_tls_and_no_starttls(self) -> None:
        log: list[tuple[str, tuple[Any, ...]]] = []
        ssl_cls = _make_fake_smtp_cls(log)
        plain_cls = _make_fake_smtp_cls([])  # should never be used

        n = SmtpNotifier(
            host="smtp.example.com",
            port=465,
            user="u",
            password="p",
            from_addr="f@x",
            to_addr="t@x",
            smtp_ssl_cls=ssl_cls,
            smtp_cls=plain_cls,
        )
        n.send(NotificationMessage(event="e", subject="s", body="b"))

        op_names = [name for name, _ in log]
        assert "init" in op_names
        assert "starttls" not in op_names  # implicit TLS, not STARTTLS
        assert "login" in op_names
        assert "send_message" in op_names

    def test_port_587_uses_starttls(self) -> None:
        log: list[tuple[str, tuple[Any, ...]]] = []
        plain_cls = _make_fake_smtp_cls(log)

        n = SmtpNotifier(
            host="smtp.example.com",
            port=587,
            user="u",
            password="p",
            from_addr="f@x",
            to_addr="t@x",
            smtp_cls=plain_cls,
            smtp_ssl_cls=_make_fake_smtp_cls([]),
        )
        n.send(NotificationMessage(event="e", subject="s", body="b"))

        op_names = [name for name, _ in log]
        # starttls must happen before login
        assert op_names.index("starttls") < op_names.index("login")

    def test_port_25_plain_no_login_when_user_empty(self) -> None:
        log: list[tuple[str, tuple[Any, ...]]] = []
        plain_cls = _make_fake_smtp_cls(log)

        n = SmtpNotifier(
            host="localhost",
            port=25,
            user="",
            password="",
            from_addr="f@x",
            to_addr="t@x",
            smtp_cls=plain_cls,
            smtp_ssl_cls=_make_fake_smtp_cls([]),
        )
        n.send(NotificationMessage(event="e", subject="s", body="b"))

        op_names = [name for name, _ in log]
        assert "starttls" not in op_names
        assert "login" not in op_names
        assert "send_message" in op_names

    def test_build_message_adds_png_attachment(self, tmp_path: Path) -> None:
        png = tmp_path / "snap.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n\x00" * 3)
        n = SmtpNotifier(
            host="smtp.example.com",
            port=465,
            user="u",
            password="p",
            from_addr="f@x",
            to_addr="t@x",
            smtp_ssl_cls=_make_fake_smtp_cls([]),
        )
        msg = NotificationMessage(
            event="e",
            subject="s",
            body="hello",
            email_attachments=[("snap.png", png)],
        )
        em = n._build_message(msg)
        names = [p.get_filename() for p in em.iter_attachments()]
        assert "snap.png" in names


# -----------------------------------------------------------------------------
# build_notifier
# -----------------------------------------------------------------------------


class TestBuildNotifier:
    def _settings(self, **overrides: Any) -> Settings:
        # Construct with empty .env + no env bleed-through. Tests set secrets
        # explicitly via the `overrides` kwarg.
        base: dict[str, Any] = {
            "tz": "America/Los_Angeles",
            "watcher_mode": "once",
            "watcher_config": "config.yaml",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "twilio_from": "",
            "notify_to_e164": "",
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
            "notify_to_email": "",
            "openai_api_key": "",
            "openrouter_api_key": "",
        }
        base.update(overrides)
        return Settings(**base)

    def test_drops_twilio_when_creds_empty(self) -> None:
        cfg = NotifyConfig(channels=["twilio", "smtp"], on_events=[])
        settings = self._settings(
            smtp_host="smtp.example.com",
            smtp_from="f@x",
            notify_to_email="t@x",
        )
        fan = build_notifier(cfg, settings)
        assert fan.channel_names == ["smtp"]

    def test_drops_smtp_when_creds_empty(self) -> None:
        cfg = NotifyConfig(channels=["twilio", "smtp"], on_events=[])
        settings = self._settings(
            twilio_account_sid="AC123",
            twilio_auth_token="tok",
            twilio_from="+1",
            notify_to_e164="+2",
        )
        fan = build_notifier(cfg, settings, twilio_client=_FakeTwilioClient())
        assert fan.channel_names == ["twilio"]

    def test_empty_result_when_nothing_configured(self) -> None:
        cfg = NotifyConfig(channels=["twilio", "smtp"], on_events=[])
        settings = self._settings()
        fan = build_notifier(cfg, settings)
        assert fan.channel_names == []

    def test_injected_twilio_client_used_even_when_creds_empty(self) -> None:
        """Lets `test-notify` smoke-test the Twilio path in CI without creds."""
        cfg = NotifyConfig(channels=["twilio"], on_events=[])
        settings = self._settings()
        fan = build_notifier(
            cfg, settings, twilio_client=_FakeTwilioClient()
        )
        assert fan.channel_names == ["twilio"]

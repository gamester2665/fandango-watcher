"""Notification channels (Twilio SMS + SMTP email).

Each ``Notifier`` implementation is synchronous, short-lived per call, and
raises on delivery failure. ``FanOutNotifier`` wraps a list of them and
collects per-channel outcomes so the caller can surface partial failures
without blocking on any one channel.

``build_notifier`` silently drops channels whose env-var secrets are blank;
this lets local dev work with only SMTP (for example) without failing fast
on missing Twilio creds.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

from .config import NotifyConfig, Settings, plain_secret

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)


# SMS has a 1600-character hard cap; give ourselves headroom for the
# "[event] subject\n\n" prefix we add before body.
_SMS_MAX_CHARS = 1400


@dataclass
class NotificationMessage:
    """One outbound notification, independent of channel."""

    event: str
    subject: str
    body: str
    # (filename, path) pairs for :class:`SmtpNotifier` only — Twilio ignores them.
    email_attachments: list[tuple[str, Path]] = field(default_factory=list)


class Notifier(ABC):
    """Base class for a single-channel notifier."""

    @property
    @abstractmethod
    def name(self) -> str:  # pragma: no cover — trivial
        ...

    @abstractmethod
    def send(self, msg: NotificationMessage) -> None: ...


# -----------------------------------------------------------------------------
# Twilio (SMS)
# -----------------------------------------------------------------------------


class TwilioNotifier(Notifier):
    """Send SMS via Twilio's REST API.

    The ``twilio`` client is imported lazily so tests that monkeypatch
    ``twilio.rest.Client`` don't have to intercept package-import time.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        client: object | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            from twilio.rest import Client  # local import; see class docstring

            self._client = Client(account_sid, auth_token)
        self._from = from_number
        self._to = to_number

    @property
    def name(self) -> str:
        return "twilio"

    def send(self, msg: NotificationMessage) -> None:
        if msg.email_attachments:
            logger.debug(
                "twilio: ignoring %d attachment(s) (SMS is text-only)",
                len(msg.email_attachments),
            )
        body = f"[{msg.event}] {msg.subject}\n\n{msg.body}"
        if len(body) > _SMS_MAX_CHARS:
            body = body[: _SMS_MAX_CHARS - 3] + "..."
        # ``client.messages.create`` raises ``TwilioRestException`` on failure;
        # letting it propagate lets FanOutNotifier record the channel failure.
        self._client.messages.create(  # type: ignore[attr-defined]
            body=body, from_=self._from, to=self._to
        )


# -----------------------------------------------------------------------------
# SMTP (email)
# -----------------------------------------------------------------------------


class SmtpNotifier(Notifier):
    """Send email via SMTPS (port 465), STARTTLS (587), or plain SMTP.

    Port selection follows RFC convention:

    * 465 -> implicit TLS via :class:`smtplib.SMTP_SSL`
    * 587 -> plain connect then ``STARTTLS``
    * anything else -> plain SMTP (useful for local MailHog-style traps)
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        to_addr: str,
        timeout_seconds: float = 30.0,
        smtp_ssl_cls: type = smtplib.SMTP_SSL,
        smtp_cls: type = smtplib.SMTP,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._to = to_addr
        self._timeout = timeout_seconds
        # Injected for unit tests. Default to stdlib.
        self._smtp_ssl_cls = smtp_ssl_cls
        self._smtp_cls = smtp_cls

    @property
    def name(self) -> str:
        return "smtp"

    def _build_message(self, msg: NotificationMessage) -> EmailMessage:
        em = EmailMessage()
        em["From"] = self._from
        em["To"] = self._to
        em["Subject"] = f"[fandango_watcher] {msg.subject}"
        em.set_content(f"Event: {msg.event}\n\n{msg.body}\n")
        for filename, path in msg.email_attachments:
            if not path.is_file():
                logger.warning("skip missing email attachment %s", path)
                continue
            data = path.read_bytes()
            suf = path.suffix.lower()
            if suf == ".png":
                main, sub = "image", "png"
            elif suf in (".jpg", ".jpeg"):
                main, sub = "image", "jpeg"
            elif suf == ".webm":
                main, sub = "video", "webm"
            else:
                main, sub = "application", "octet-stream"
            em.add_attachment(
                data, maintype=main, subtype=sub, filename=filename or path.name
            )
        return em

    def send(self, msg: NotificationMessage) -> None:
        em = self._build_message(msg)
        if self._port == 465:
            with self._smtp_ssl_cls(
                self._host, self._port, timeout=self._timeout
            ) as s:
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(em)
        else:
            with self._smtp_cls(self._host, self._port, timeout=self._timeout) as s:
                if self._port == 587:
                    s.starttls()
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(em)


# -----------------------------------------------------------------------------
# Fan-out
# -----------------------------------------------------------------------------


@dataclass
class ChannelResult:
    name: str
    ok: bool
    error: BaseException | None = None


class FanOutNotifier:
    """Send the same ``NotificationMessage`` to every wrapped notifier.

    One channel failing does NOT short-circuit the rest; each exception is
    captured into the returned ``ChannelResult`` list. The loop can then
    decide whether any failure is worth logging / re-raising.
    """

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = list(notifiers)

    @property
    def channel_names(self) -> list[str]:
        return [n.name for n in self._notifiers]

    def send(self, msg: NotificationMessage) -> list[ChannelResult]:
        results: list[ChannelResult] = []
        for n in self._notifiers:
            try:
                n.send(msg)
                results.append(ChannelResult(name=n.name, ok=True))
            except Exception as e:  # noqa: BLE001 — we must catch every channel error
                logger.exception("notifier %s failed", n.name)
                results.append(ChannelResult(name=n.name, ok=False, error=e))
        return results


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------


def _twilio_creds_present(settings: Settings) -> bool:
    return bool(
        settings.twilio_account_sid
        and plain_secret(settings.twilio_auth_token).strip()
        and settings.twilio_from
        and settings.notify_to_e164
    )


def _smtp_creds_present(settings: Settings) -> bool:
    # Host + from + to are minimum viable. Username/password are optional
    # for local/relay setups.
    return bool(settings.smtp_host and settings.smtp_from and settings.notify_to_email)


def build_notifier(
    cfg: NotifyConfig,
    settings: Settings,
    *,
    twilio_client: object | None = None,
    smtp_ssl_cls: type = smtplib.SMTP_SSL,
    smtp_cls: type = smtplib.SMTP,
) -> FanOutNotifier:
    """Assemble a ``FanOutNotifier`` from config + env-backed settings.

    Channels whose credentials are missing are silently dropped with a
    WARN-level log line. Callers that require at least one live channel
    should check ``FanOutNotifier.channel_names`` after construction.
    """
    notifiers: list[Notifier] = []
    for channel in cfg.channels:
        if channel == "twilio":
            if _twilio_creds_present(settings) or twilio_client is not None:
                notifiers.append(
                    TwilioNotifier(
                        account_sid=settings.twilio_account_sid,
                        auth_token=plain_secret(settings.twilio_auth_token),
                        from_number=settings.twilio_from,
                        to_number=settings.notify_to_e164,
                        client=twilio_client,
                    )
                )
            else:
                logger.warning(
                    "twilio channel configured but env vars incomplete; skipping"
                )
        elif channel == "smtp":
            if _smtp_creds_present(settings):
                notifiers.append(
                    SmtpNotifier(
                        host=settings.smtp_host,
                        port=settings.smtp_port,
                        user=settings.smtp_user,
                        password=plain_secret(settings.smtp_password),
                        from_addr=settings.smtp_from,
                        to_addr=settings.notify_to_email,
                        smtp_ssl_cls=smtp_ssl_cls,
                        smtp_cls=smtp_cls,
                    )
                )
            else:
                logger.warning(
                    "smtp channel configured but env vars incomplete; skipping"
                )
    return FanOutNotifier(notifiers)

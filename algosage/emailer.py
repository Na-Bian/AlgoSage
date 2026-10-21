"""Bounded email delivery adapters for AlgoSage authentication.

The authentication service owns rate limits and verification state. These
adapters only turn an already-validated message into SMTP MIME or a local
development outbox file. Secrets and verification codes are never logged here.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .runtime_paths import runtime_path


@dataclass(frozen=True)
class EmailMessage:
    to_address: str
    subject: str
    text_body: str
    html_body: str | None = None


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Deliver one message; raise on an infrastructure failure."""


class OutboxEmailSender:
    """Development-only delivery to a protected local directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if os.stat(self.root).st_mode & 0o077:
            raise ValueError("email outbox must not be group/world accessible")

    async def send(self, message: EmailMessage) -> None:
        mail = _mime_message(message, from_address="AlgoSage development outbox")
        target = self.root / f"{uuid4()}.eml"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(mail.as_bytes())
        temporary.replace(target)


class SMTPEmailSender:
    """Small blocking SMTP adapter, executed outside the event loop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool,
        timeout: float = 10,
        use_auth: bool = True,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_auth = use_auth
        if self.use_auth and (not username or not password):
            raise ValueError("SMTP authentication requires both username and password")
        self.from_address = from_address
        self.use_tls = use_tls
        self.timeout = timeout

    def connection_info(self) -> dict[str, str | int | bool]:
        """Return non-secret settings for diagnostics and startup checks."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "use_auth": self.use_auth,
            "from_address": self.from_address,
            "use_tls": self.use_tls,
            "timeout_seconds": self.timeout,
        }

    async def send(self, message: EmailMessage) -> None:
        mail = _mime_message(message, from_address=self.from_address)
        await asyncio.to_thread(self._send_sync, mail)

    def _send_sync(self, mail: EmailMessage) -> None:
        tls_context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout,
                context=tls_context,
            ) as client:
                if self.use_auth:
                    client.login(self.username, self.password)
                client.send_message(mail)
            return
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            client.ehlo()
            if self.use_tls:
                client.starttls(context=tls_context)
                client.ehlo()
            if self.use_auth:
                client.login(self.username, self.password)
            client.send_message(mail)

    def verify(self) -> None:
        """Open, authenticate, and close an SMTP connection."""
        asyncio.run(asyncio.to_thread(self._verify_sync))

    def _verify_sync(self) -> None:
        tls_context = ssl.create_default_context()
        if self.port == 465:
            client = smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout,
                context=tls_context,
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)

        with client:
            client.ehlo()
            if self.port != 465 and self.use_tls:
                client.starttls(context=tls_context)
                client.ehlo()
            if self.use_auth:
                client.login(self.username, self.password)


def _setting(name: str, default: str = "") -> str:
    """Read a SAGE setting with read-only compatibility for legacy EG names."""
    value = os.environ.get(name)
    if value:
        return value
    if name.startswith("SAGE_"):
        return os.environ.get(f"EG_{name[5:]}", default)
    return default


def _mime_message(message: EmailMessage, *, from_address: str) -> EmailMessage:
    mail = MimeEmailMessage()
    mail["From"] = from_address
    mail["To"] = message.to_address
    mail["Subject"] = message.subject
    mail.set_content(message.text_body, charset="utf-8")
    if message.html_body:
        mail.add_alternative(message.html_body, subtype="html", charset="utf-8")
    return mail


def email_sender_from_env() -> EmailSender:
    mode = _setting("SAGE_EMAIL_SENDER", "outbox").strip().lower()
    if mode == "outbox":
        root = Path(
            runtime_path(
                "SAGE_EMAIL_OUTBOX_DIR",
                _setting("SAGE_EMAIL_OUTBOX_DIR", ".algosage/dev-outbox"),
            )
        )
        return OutboxEmailSender(root)
    if mode == "smtp":
        required = {
            "SAGE_SMTP_HOST": _setting("SAGE_SMTP_HOST"),
            "SAGE_SMTP_PORT": _setting("SAGE_SMTP_PORT"),
            "SAGE_EMAIL_FROM": _setting("SAGE_EMAIL_FROM"),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError("missing SMTP settings: " + ", ".join(missing))
        return SMTPEmailSender(
            host=required["SAGE_SMTP_HOST"],
            port=int(required["SAGE_SMTP_PORT"]),
            username=_setting("SAGE_SMTP_USERNAME"),
            password=_setting("SAGE_SMTP_PASSWORD"),
            use_auth=_setting("SAGE_SMTP_AUTH", "true").lower() in {"true", "1", "yes"},
            from_address=required["SAGE_EMAIL_FROM"],
            use_tls=_setting("SAGE_SMTP_USE_TLS", "true").lower()
            in {"true", "1", "yes"},
            timeout=float(_setting("SAGE_SMTP_TIMEOUT_SECONDS", "10")),
        )
    raise ValueError("SAGE_EMAIL_SENDER must be 'outbox' or 'smtp'")

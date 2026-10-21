import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from algosage.emailer import (
    EmailMessage,
    OutboxEmailSender,
    SMTPEmailSender,
    email_sender_from_env,
)


def message():
    return EmailMessage(
        to_address="student@hust.edu.cn",
        subject="AlgoSage check",
        text_body="check",
    )


def smtp_client_mock():
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


class SMTPEmailerTests(unittest.TestCase):
    def test_port_465_uses_verified_smtp_ssl_and_sends_mime_message(self):
        client = smtp_client_mock()
        factory = MagicMock(return_value=client)
        sender = SMTPEmailSender(
            host="smtp.example.edu",
            port=465,
            username="algo",
            password="secret",
            from_address="AlgoSage <no-reply@example.edu>",
            use_tls=True,
        )
        with patch("algosage.emailer.smtplib.SMTP_SSL", factory) as mocked:
            asyncio_run(sender.send(message()))

        mocked.assert_called_once()
        self.assertIn("context", mocked.call_args.kwargs)
        client.login.assert_called_once_with("algo", "secret")
        client.send_message.assert_called_once()
        mail = client.send_message.call_args.args[0]
        self.assertEqual(mail["To"], "student@hust.edu.cn")
        self.assertEqual(mail["Subject"], "AlgoSage check")

    def test_port_587_uses_starttls_before_authentication(self):
        client = smtp_client_mock()
        factory = MagicMock(return_value=client)
        sender = SMTPEmailSender(
            host="smtp.example.edu",
            port=587,
            username="algo",
            password="secret",
            from_address="AlgoSage <no-reply@example.edu>",
            use_tls=True,
        )
        with patch("algosage.emailer.smtplib.SMTP", factory) as mocked:
            asyncio_run(sender.send(message()))

        mocked.assert_called_once()
        client.starttls.assert_called_once()
        self.assertIn("context", client.starttls.call_args.kwargs)
        client.login.assert_called_once_with("algo", "secret")
        client.send_message.assert_called_once()

    def test_relay_can_send_without_authentication(self):
        client = smtp_client_mock()
        factory = MagicMock(return_value=client)
        sender = SMTPEmailSender(
            host="relay.example.edu",
            port=25,
            username="",
            password="",
            from_address="AlgoSage <no-reply@example.edu>",
            use_tls=False,
            use_auth=False,
        )
        with patch("algosage.emailer.smtplib.SMTP", factory) as mocked:
            asyncio_run(sender.send(message()))

        mocked.assert_called_once()
        client.login.assert_not_called()
        client.starttls.assert_not_called()
        client.send_message.assert_called_once()

    def test_verify_opens_and_authenticates_without_sending(self):
        client = smtp_client_mock()
        sender = SMTPEmailSender(
            host="smtp.example.edu",
            port=587,
            username="algo",
            password="secret",
            from_address="AlgoSage <no-reply@example.edu>",
            use_tls=True,
        )
        with patch("algosage.emailer.smtplib.SMTP", return_value=client) as mocked:
            sender.verify()

        mocked.assert_called_once()
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("algo", "secret")
        client.send_message.assert_not_called()

    def test_environment_supports_new_and_legacy_prefixes(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {
                "SAGE_EMAIL_SENDER": "outbox",
                "SAGE_EMAIL_OUTBOX_DIR": str(Path(root) / "new"),
            }):
                sender = email_sender_from_env()
                self.assertEqual(type(sender).__name__, "OutboxEmailSender")

            env = {
                "SAGE_EMAIL_SENDER": "smtp",
                "EG_EMAIL_SENDER": "outbox",
                "SAGE_SMTP_HOST": "smtp.example.edu",
                "SAGE_SMTP_PORT": "587",
                "SAGE_SMTP_USERNAME": "algo",
                "SAGE_SMTP_PASSWORD": "secret",
                "SAGE_EMAIL_FROM": "AlgoSage <no-reply@example.edu>",
            }
            with patch.dict(os.environ, env, clear=False):
                sender = email_sender_from_env()
                self.assertEqual(type(sender).__name__, "SMTPEmailSender")

            legacy_env = {
                "EG_EMAIL_SENDER": "smtp",
                "SAGE_SMTP_HOST": "",
                "EG_SMTP_HOST": "legacy.example.edu",
                "SAGE_SMTP_PORT": "465",
                "SAGE_SMTP_USERNAME": "legacy",
                "SAGE_SMTP_PASSWORD": "legacy-secret",
                "SAGE_EMAIL_FROM": "AlgoSage <no-reply@example.edu>",
            }
            with patch.dict(os.environ, legacy_env, clear=False):
                sender = email_sender_from_env()
                self.assertEqual(sender.host, "legacy.example.edu")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from examguard.emailer import OutboxEmailSender
from examguard.student_auth import (
    AuthCodeError,
    AuthConfig,
    AuthCredentialsError,
    AuthRateLimitError,
    AuthValidationError,
    StudentAuthService,
    StudentAuthStore,
)


@dataclass
class FakeEmailSender:
    messages: list = field(default_factory=list)

    async def send(self, message):
        self.messages.append(message)


def run(coro):
    return asyncio.run(coro)


def service(tmp: Path, sender=None, **overrides):
    values = {
        "account_hourly_limit": 5,
        "ip_hourly_limit": 5,
        "resend_interval_seconds": 60,
    }
    values.update(overrides)
    return StudentAuthService(
        store=StudentAuthStore(str(tmp / "auth.sqlite3")),
        email_sender=sender or FakeEmailSender(),
        config=AuthConfig(mode="hust", **values),
        code_pepper="unit-test-code-pepper-with-more-than-32-chars",
    )


def code_from(message):
    return message.text_body.split("：", 1)[1].split("\n", 1)[0]


class StudentAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sender = FakeEmailSender()
        self.auth = service(self.root, self.sender)

    def test_registration_code_creates_verified_account(self):
        run(
            self.auth.request_registration_code(
                nickname="张三",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.sender.messages[0].to_address, "U202113566@hust.edu.cn")
        code = code_from(self.sender.messages[0])
        account = self.auth.confirm_registration(student_id="u202113566", code=code)
        self.assertEqual(account.identifier, "student:U202113566")
        self.assertEqual(account.nickname, "张三")

    def test_registration_rejects_bad_format_and_weak_password(self):
        with self.assertRaises(AuthValidationError):
            run(
                self.auth.request_registration_code(
                    nickname="x",
                    student_id="2021",
                    password="correct-horse-battery",
                    ip="127.0.0.1",
                )
            )
        with self.assertRaises(AuthValidationError):
            run(
                self.auth.request_registration_code(
                    nickname="U202113566",
                    student_id="U202113566",
                    password="short",
                    ip="127.0.0.1",
                )
            )
        self.assertEqual(self.sender.messages, [])

    def test_registration_resend_is_limited(self):
        inputs = dict(
            nickname="张三",
            student_id="U202113566",
            password="correct-horse-battery",
            ip="127.0.0.1",
        )
        run(self.auth.request_registration_code(**inputs))
        with self.assertRaises(AuthRateLimitError):
            run(self.auth.request_registration_code(**inputs))

    def test_wrong_or_consumed_code_is_rejected(self):
        run(
            self.auth.request_registration_code(
                nickname="张三",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        with self.assertRaises(AuthCodeError):
            run(self.auth.confirm_registration(student_id="U202113566", code="000000"))

    def test_login_locks_after_repeated_failures(self):
        run(
            self.auth.request_registration_code(
                nickname="张三",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        code = code_from(self.sender.messages[0])
        self.auth.confirm_registration(student_id="U202113566", code=code)
        for _ in range(4):
            self.assertIsNone(self.auth.authenticate("U202113566", "wrong-password"))
        with self.assertRaises(AuthCredentialsError):
            self.auth.authenticate("U202113566", "wrong-password")
        self.assertTrue(self.auth.store.get_account("U202113566")["locked_until"])

    def test_password_reset_changes_password_and_unknown_is_generic(self):
        run(
            self.auth.request_registration_code(
                nickname="张三",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        self.auth.confirm_registration(
            student_id="U202113566", code=code_from(self.sender.messages[0])
        )
        run(self.auth.request_password_reset_code(student_id="U202113566", ip="127.0.0.1"))
        reset_code = code_from(self.sender.messages[-1])
        self.auth.confirm_password_reset(
            student_id="U202113566",
            code=reset_code,
            new_password="another-long-passphrase",
        )
        self.assertIsNotNone(self.auth.authenticate("U202113566", "another-long-passphrase"))
        self.sender.messages.clear()
        run(self.auth.request_password_reset_code(student_id="U202119999", ip="127.0.0.1"))
        self.assertEqual(self.sender.messages, [])
        self.auth.store.close()

    def test_outbox_is_not_group_or_world_accessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = OutboxEmailSender(Path(tmp) / "outbox")
            self.assertEqual((Path(tmp) / "outbox").stat().st_mode & 0o077, 0)


class StudentAuthRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_registration_and_reset_routes_use_same_origin_json(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from examguard.student_auth import install_student_auth

        app = FastAPI()
        sender = FakeEmailSender()
        auth = service(self.root, sender)
        install_student_auth(app, lambda: auth)
        client = TestClient(app)

        page = client.get("/auth/hust/register")
        self.assertEqual(page.status_code, 200)
        response = client.post(
            "/auth/hust/register/request-code",
            json={
                "nickname": "张三",
                "student_id": "U202113566",
                "password": "correct-horse-battery",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "sent"})
        confirmed = client.post(
            "/auth/hust/register/confirm",
            json={
                "student_id": "U202113566",
                "code": code_from(sender.messages[0]),
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["identifier"], "student:U202113566")

        reset_requested = client.post(
            "/auth/hust/password-reset/request-code",
            json={"student_id": "U202113566"},
        )
        self.assertEqual(reset_requested.status_code, 200)
        reset_confirmed = client.post(
            "/auth/hust/password-reset/confirm",
            json={
                "student_id": "U202113566",
                "code": code_from(sender.messages[-1]),
                "new_password": "another-long-passphrase",
            },
        )
        self.assertEqual(reset_confirmed.status_code, 200)
        self.assertIsNotNone(
            auth.authenticate("U202113566", "another-long-passphrase")
        )

        unknown = client.post(
            "/auth/hust/password-reset/request-code",
            json={"student_id": "U202119999"},
        )
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(len(sender.messages), 2)

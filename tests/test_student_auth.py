import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from fastapi.testclient import TestClient

from algosage.emailer import OutboxEmailSender
from algosage.student_auth import (
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

        from algosage.student_auth import install_student_auth

        app = FastAPI()
        sender = FakeEmailSender()
        auth = service(self.root, sender)
        install_student_auth(app, lambda: auth)
        client = TestClient(app)

        page = client.get("/auth/hust/register", follow_redirects=False)
        self.assertEqual(page.status_code, 301)
        self.assertEqual(page.headers["location"], "/register")
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


class StudentProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sender = FakeEmailSender()
        self.auth = service(self.root, self.sender)
        # Create one account directly through the service, without waiting for email.
        run(
            self.auth.request_registration_code(
                nickname="验收同学",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        self.auth.confirm_registration(
            student_id="U202113566", code=code_from(self.sender.messages[0])
        )
        secret = mock.patch.dict(
            "os.environ", {"CHAINLIT_AUTH_SECRET": "profile-route-secret-value-32-chars"}
        )
        secret.start()
        self.addCleanup(secret.stop)
        from fastapi import FastAPI

        from algosage.student_auth import install_student_auth

        self.app = FastAPI()
        install_student_auth(self.app, lambda: self.auth)

    def test_profile_view_and_updates(self):
        from chainlit.server import get_current_user
        from fastapi.testclient import TestClient

        alice = SimpleNamespace(identifier="student:U202113566")
        client = TestClient(self.app)
        self.app.dependency_overrides[get_current_user] = lambda: alice
        self.addCleanup(self.app.dependency_overrides.clear)

        profile = client.get("/auth/hust/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["student_id"], "U202113566")
        self.assertEqual(profile.json()["email"], "U202113566@hust.edu.cn")

        renamed = client.patch(
            "/auth/hust/profile", json={"nickname": "新昵称"}
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["nickname"], "新昵称")
        self.assertEqual(
            self.auth.store.get_account("U202113566")["nickname"], "新昵称"
        )

        password_changed = client.patch(
            "/auth/hust/profile",
            json={
                "nickname": "新昵称",
                "current_password": "correct-horse-battery",
                "new_password": "another-long-passphrase",
            },
        )
        self.assertEqual(password_changed.status_code, 200)
        self.assertEqual(password_changed.json()["session_version"], 2)
        self.assertIsNotNone(
            self.auth.authenticate("U202113566", "another-long-passphrase")
        )

        denied = client.patch(
            "/auth/hust/profile",
            json={
                "nickname": "新昵称",
                "current_password": "wrong-current-password",
                "new_password": "another-long-passphrase",
            },
        )
        self.assertEqual(denied.status_code, 423)


class ProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sender = FakeEmailSender()
        self.auth = service(self.root, self.sender)
        secret = mock.patch.dict(
            "os.environ", {"CHAINLIT_AUTH_SECRET": "profile-route-secret-value-32-chars"}
        )
        secret.start()
        self.addCleanup(secret.stop)
        run(
            self.auth.request_registration_code(
                nickname="验收同学",
                student_id="U202113566",
                password="correct-horse-battery",
                ip="127.0.0.1",
            )
        )
        self.auth.confirm_registration(
            student_id="U202113566",
            code=code_from(self.sender.messages[0]),
        )
        from fastapi import FastAPI

        from algosage.student_auth import install_profile_routes

        self.app = FastAPI()
        install_profile_routes(self.app, lambda: self.auth)
        self.client = TestClient(self.app)
        current = SimpleNamespace(identifier="student:U202113566")
        self.app.dependency_overrides[
            self.current_user_dependency()
        ] = lambda: current
        self.addCleanup(self.app.dependency_overrides.clear)

    def current_user_dependency(self):
        from chainlit.server import get_current_user

        return get_current_user

    def test_profile_read_update_and_avatar_upload(self):
        from PIL import Image
        import io

        response = self.client.get("/auth/profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["student_id"], "U202113566")
        self.assertFalse(response.json()["has_avatar"])

        renamed = self.client.patch(
            "/auth/profile", json={"nickname": "界面验收同学"}
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["nickname"], "界面验收同学")

        image = Image.new("RGB", (700, 400), "#4d6bfe")
        output = io.BytesIO()
        image.save(output, format="PNG")
        uploaded = self.client.put(
            "/auth/avatar",
            content=output.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertTrue(uploaded.json()["has_avatar"])
        avatar = self.client.get("/auth/avatar/me")
        self.assertEqual(avatar.status_code, 200)
        self.assertEqual(avatar.headers["content-type"], "image/png")
        self.assertEqual(self.auth.store.get_avatar("U202113566")[1], "image/png")

        page = self.client.get("/profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn("个人信息", page.text)
        self.assertIn("profile-page.css", page.text)

    def test_profile_page_redirects_unauthenticated_user(self):
        self.app.dependency_overrides[self.current_user_dependency()] = lambda: None
        response = self.client.get("/profile", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_legacy_environment_variables_are_promoted(self):
        from algosage.config import Settings

        with mock.patch.dict(
            os.environ,
            {"EG_CLASSIFIER_MODE": "hybrid", "SAGE_CLASSIFIER_MODE": ""},
            clear=False,
        ):
            settings = Settings.from_env()
            self.assertEqual(settings.classifier_mode, "hybrid")

    def test_custom_auth_pages_replace_chainlit_login(self):
        for path, mode in [("/login", "login"), ("/register", "register"), ("/forgot-password", "reset")]:
            self.app.dependency_overrides[self.current_user_dependency()] = lambda: None
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn(f'data-view="{mode}"', response.text)
            self.assertIn("AlgoSage", response.text)
            self.assertIn("/public/brand-logo.png", response.text)
            self.assertNotIn('class="eg-auth-tabs"', response.text)
            if path == "/login":
                self.assertIn('id="login-feedback" class="login-feedback" data-visible="false"', response.text)
                self.assertIn('id="forgot-link" class="forgot-link" href="/forgot-password" hidden', response.text)
                self.assertIn(">登录 AlgoSage</h1>", response.text)
            if path == "/register":
                self.assertIn(">注册 AlgoSage</h1>", response.text)
                self.assertIn("请输入昵称", response.text)
                self.assertIn("请输入学号", response.text)
                self.assertIn("请输入密码", response.text)
                self.assertIn("请再次输入密码", response.text)
                self.assertIn("请输入验证码", response.text)
                self.assertIn("验证码将发送至该学号绑定的教育邮箱", response.text)
                self.assertNotIn('for="register-email"', response.text)

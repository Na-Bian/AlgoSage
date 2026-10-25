import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient


class RuntimeSecurityTests(unittest.TestCase):
    def test_origin_guard_uses_explicit_allowlist_and_allows_non_browser(self):
        from algosage.runtime_security import install_origin_guard

        app = FastAPI()
        app.get("/health")(lambda: {"status": "ok"})
        with mock.patch.dict(
            os.environ,
            {
                "SAGE_ALLOWED_ORIGINS": "https://algosage.example,https://www.algosage.example"
            },
        ):
            install_origin_guard(app)

        client = TestClient(app)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(
            client.get(
                "/health", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                "/health", headers={"Origin": "https://algosage.example"}
            ).status_code,
            200,
        )

    def test_default_origin_guard_only_allows_local_http_origins(self):
        from algosage.runtime_security import install_origin_guard

        app = FastAPI()
        app.get("/health")(lambda: {"status": "ok"})
        with mock.patch.dict(
            os.environ, {"SAGE_ALLOWED_ORIGINS": "", "CHAINLIT_PORT": "8000"}
        ):
            install_origin_guard(app)
        client = TestClient(app)
        self.assertEqual(
            client.get(
                "/health", headers={"Origin": "http://localhost:8000"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(
                "/health", headers={"Origin": "http://127.0.0.1:8000"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(
                "/health", headers={"Origin": "https://algosage.example"}
            ).status_code,
            403,
        )

    def test_secure_cookie_mode_updates_chainlit_cookie_module(self):
        from algosage.runtime_security import configure_auth_cookies

        with mock.patch.dict(
            os.environ,
            {"SAGE_SECURE_COOKIES": "true", "CHAINLIT_COOKIE_SAMESITE": "strict"},
        ):
            configure_auth_cookies()
        from chainlit.auth import cookie

        self.assertTrue(cookie._cookie_secure)
        self.assertEqual(cookie._cookie_samesite, "strict")
        with mock.patch.dict(
            os.environ,
            {"SAGE_SECURE_COOKIES": "false", "CHAINLIT_COOKIE_SAMESITE": "lax"},
        ):
            configure_auth_cookies()
        self.assertFalse(cookie._cookie_secure)
        self.assertEqual(cookie._cookie_samesite, "lax")

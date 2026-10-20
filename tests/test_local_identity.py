import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LocalIdentityTests(unittest.TestCase):
    def test_default_secret_uses_app_root_instead_of_launch_directory(self):
        from examguard.local_identity import ensure_local_secret

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            with patch.dict(os.environ, {"CHAINLIT_APP_ROOT": root}, clear=True):
                previous = Path.cwd()
                try:
                    os.chdir(cwd)
                    ensure_local_secret()
                    self.assertTrue((Path(root) / ".examguard/auth.secret").is_file())
                    self.assertFalse((Path(cwd) / ".examguard/auth.secret").exists())
                finally:
                    os.chdir(previous)

    def test_secret_persists_with_private_permissions(self):
        from examguard.local_identity import ensure_local_secret

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {}, clear=True
        ):
            path = Path(tmp) / "auth.secret"
            first = ensure_local_secret(path)
            os.environ.pop("CHAINLIT_AUTH_SECRET")
            self.assertEqual(first, ensure_local_secret(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertGreaterEqual(len(first), 40)

    def test_cookie_identity_reused_but_untrusted_header_ignored(self):
        from examguard.local_identity import browser_user
        from chainlit.auth import create_jwt

        with patch.dict(os.environ, {"CHAINLIT_AUTH_SECRET": "test-secret-only" * 4}):
            first = browser_user({"x-user": "admin", "authorization": "Bearer forged"})
            self.assertTrue(first.identifier.startswith("browser:"))
            token = create_jwt(first)
            self.assertEqual(
                browser_user({"cookie": "access_token=" + token}).identifier,
                first.identifier,
            )
            self.assertNotEqual(
                browser_user({"cookie": "access_token=forged"}).identifier,
                first.identifier,
            )
            self.assertNotIn("admin", first.identifier)


class DurableBrowserIdentityTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI, Request, Response
        from fastapi.testclient import TestClient
        from chainlit.auth import create_jwt, decode_jwt, get_token_from_cookies, set_auth_cookie
        from examguard.local_identity import browser_user, install_browser_identity

        env = patch.dict(os.environ, {"CHAINLIT_AUTH_SECRET": "test-secret-only" * 4})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        install_browser_identity(app)

        @app.post("/auth/header")
        async def auth(request: Request):
            user = browser_user(request.headers)
            response = Response()
            set_auth_cookie(request, response, create_jwt(user))
            return response

        @app.get("/user")
        async def user(request: Request):
            try:
                current = decode_jwt(get_token_from_cookies(request.cookies))
            except Exception:
                return Response(status_code=401)
            return {"identifier": current.identifier}

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def login(self):
        self.client.post("/auth/header").raise_for_status()
        return self.client.get("/user").json()["identifier"]

    def test_missing_or_expired_login_restores_original_workspace(self):
        import jwt
        first = self.login()
        self.assertIn("eg_workspace", self.client.cookies)
        self.client.cookies.delete("access_token")
        self.assertEqual(self.client.get("/user").status_code, 401)
        self.assertEqual(self.login(), first)
        expired = jwt.encode({"identifier": first, "exp": 1},
                             os.environ["CHAINLIT_AUTH_SECRET"], algorithm="HS256")
        self.client.cookies.set("access_token", expired, domain="testserver.local", path="/")
        self.assertEqual(self.login(), first)

    def test_existing_valid_login_wins_and_migrates_without_changing_owner(self):
        from chainlit.auth import create_jwt
        from chainlit.user import User

        first = self.login()
        owner = "browser:11111111-1111-4111-8111-111111111111"
        self.assertNotEqual(first, owner)
        legacy = create_jwt(User(identifier=owner))
        self.client.cookies.set("access_token", legacy, domain="testserver.local", path="/")
        self.client.get("/user").raise_for_status()
        self.client.cookies.delete("access_token")
        self.assertEqual(self.login(), owner)

    def test_tampered_expired_and_wrong_audience_workspace_tokens_do_not_select_owner(self):
        import jwt

        for variant in ("forged", "expired", "wrong-audience", "non-browser", "missing-exp"):
            with self.subTest(variant=variant):
                self.client.cookies.clear()
                owner = self.login()
                payload = {"sub": owner, "aud": "examguard.local-workspace.v1", "exp": 4102444800}
                key = os.environ["CHAINLIT_AUTH_SECRET"]
                if variant == "forged":
                    key = "untrusted-key" * 4
                elif variant == "expired":
                    payload["exp"] = 1
                elif variant == "wrong-audience":
                    payload["aud"] = "other-app"
                elif variant == "non-browser":
                    payload["sub"] = "admin"
                else:
                    payload.pop("exp")
                self.client.cookies.delete("access_token")
                token = jwt.encode(payload, key, algorithm="HS256")
                self.client.cookies.set("eg_workspace", token, domain="testserver.local", path="/")
                self.assertNotEqual(self.login(), owner)

    def test_workspace_credential_cannot_be_used_as_api_login_token(self):
        self.login()
        token = self.client.cookies["eg_workspace"]
        self.client.cookies.set("access_token", token, domain="testserver.local", path="/")
        self.assertEqual(self.client.get("/user").status_code, 401)

    def test_workspace_cookie_is_http_only_with_long_lifetime(self):
        response = self.client.post("/auth/header")
        cookie = next(c for c in response.headers.get_list("set-cookie")
                      if c.startswith("eg_workspace="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=31536000", cookie)

    def test_soon_expiring_workspace_cookie_is_renewed_on_normal_use(self):
        import jwt
        from datetime import datetime, timedelta, timezone

        owner = self.login()
        soon = jwt.encode({"sub": owner, "aud": "examguard.local-workspace.v1",
                           "exp": datetime.now(timezone.utc) + timedelta(days=1)},
                          os.environ["CHAINLIT_AUTH_SECRET"], algorithm="HS256")
        self.client.cookies.set("eg_workspace", soon, domain="testserver.local", path="/")
        response = self.client.get("/user")
        self.assertTrue(any(c.startswith("eg_workspace=")
                            for c in response.headers.get_list("set-cookie")))
        payload = jwt.decode(self.client.cookies["eg_workspace"],
                             os.environ["CHAINLIT_AUTH_SECRET"], algorithms=["HS256"],
                             audience="examguard.local-workspace.v1")
        self.assertEqual(payload["sub"], owner)
        self.assertGreater(payload["exp"], (datetime.now(timezone.utc) + timedelta(days=360)).timestamp())

    def test_https_workspace_cookie_has_secure_flag(self):
        response = self.client.post("https://testserver/auth/header")
        cookie = next(c for c in response.headers.get_list("set-cookie")
                      if c.startswith("eg_workspace="))
        self.assertIn("; Secure", cookie)

    def test_new_browser_does_not_inherit_another_workspace(self):
        first = self.login()
        self.client.cookies.clear()
        self.assertNotEqual(self.login(), first)

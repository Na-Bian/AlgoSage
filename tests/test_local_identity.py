import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LocalIdentityTests(unittest.TestCase):
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

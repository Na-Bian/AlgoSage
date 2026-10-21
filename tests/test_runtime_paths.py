import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimePathTests(unittest.TestCase):
    def test_relative_runtime_paths_use_application_root(self):
        from algosage.runtime_paths import runtime_path

        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"CHAINLIT_APP_ROOT": root, "SAGE_CHAT_FILE": "data/chats.db"}):
                self.assertEqual(runtime_path("SAGE_CHAT_FILE", ".algosage/chats.sqlite3"),
                                 str(Path(root).resolve() / "data/chats.db"))
                self.assertEqual(runtime_path("SAGE_AUTH_SECRET_FILE", ".algosage/auth.secret"),
                                 str(Path(root).resolve() / ".algosage/auth.secret"))

    def test_explicit_absolute_path_and_disabled_state_are_preserved(self):
        from algosage.runtime_paths import runtime_path

        with patch.dict(os.environ, {"SAGE_CHAT_FILE": "/tmp/explicit/chats.db", "SAGE_STATE_FILE": ""}):
            self.assertEqual(runtime_path("SAGE_CHAT_FILE", "default"), str(Path("/tmp/explicit/chats.db").resolve()))
            self.assertEqual(runtime_path("SAGE_STATE_FILE", "default"), "")
        with patch.dict(os.environ, {"SAGE_CHAT_FILE": ":memory:"}):
            self.assertEqual(runtime_path("SAGE_CHAT_FILE", "default"), ":memory:")

    def test_legacy_examguard_runtime_directory_migrates_once(self):
        from algosage.runtime_paths import runtime_path

        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / ".examguard"
            legacy.mkdir()
            (legacy / "chats.sqlite3").write_bytes(b"legacy")
            with patch.dict(os.environ, {"CHAINLIT_APP_ROOT": root, "SAGE_CHAT_FILE": ".algosage/chats.sqlite3"}):
                migrated = Path(runtime_path("SAGE_CHAT_FILE", ".algosage/chats.sqlite3"))
                self.assertEqual(migrated.read_bytes(), b"legacy")
                self.assertTrue(migrated.exists())
                self.assertFalse(legacy.exists())

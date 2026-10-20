import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimePathTests(unittest.TestCase):
    def test_relative_runtime_paths_use_application_root(self):
        from examguard.runtime_paths import runtime_path

        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"CHAINLIT_APP_ROOT": root, "EG_CHAT_FILE": "data/chats.db"}):
                self.assertEqual(runtime_path("EG_CHAT_FILE", ".examguard/chats.sqlite3"),
                                 str(Path(root).resolve() / "data/chats.db"))
                self.assertEqual(runtime_path("EG_AUTH_SECRET_FILE", ".examguard/auth.secret"),
                                 str(Path(root).resolve() / ".examguard/auth.secret"))

    def test_explicit_absolute_path_and_disabled_state_are_preserved(self):
        from examguard.runtime_paths import runtime_path

        with patch.dict(os.environ, {"EG_CHAT_FILE": "/tmp/explicit/chats.db", "EG_STATE_FILE": ""}):
            self.assertEqual(runtime_path("EG_CHAT_FILE", "default"), str(Path("/tmp/explicit/chats.db").resolve()))
            self.assertEqual(runtime_path("EG_STATE_FILE", "default"), "")
        with patch.dict(os.environ, {"EG_CHAT_FILE": ":memory:"}):
            self.assertEqual(runtime_path("EG_CHAT_FILE", "default"), ":memory:")

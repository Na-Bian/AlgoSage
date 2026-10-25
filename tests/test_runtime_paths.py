import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimePathTests(unittest.TestCase):
    def test_relative_runtime_paths_use_application_root(self):
        from algosage.runtime_paths import runtime_path

        with tempfile.TemporaryDirectory() as root:
            with patch.dict(
                os.environ,
                {"CHAINLIT_APP_ROOT": root, "SAGE_CHAT_FILE": "data/chats.db"},
            ):
                self.assertEqual(
                    runtime_path("SAGE_CHAT_FILE", ".algosage/chats.sqlite3"),
                    str(Path(root).resolve() / "data/chats.db"),
                )
                self.assertEqual(
                    runtime_path("SAGE_AUTH_SECRET_FILE", ".algosage/auth.secret"),
                    str(Path(root).resolve() / ".algosage/auth.secret"),
                )

    def test_explicit_absolute_path_and_disabled_state_are_preserved(self):
        from algosage.runtime_paths import runtime_path

        with patch.dict(
            os.environ,
            {"SAGE_CHAT_FILE": "/tmp/explicit/chats.db", "SAGE_STATE_FILE": ""},
        ):
            self.assertEqual(
                runtime_path("SAGE_CHAT_FILE", "default"),
                str(Path("/tmp/explicit/chats.db").resolve()),
            )
            self.assertEqual(runtime_path("SAGE_STATE_FILE", "default"), "")
        with patch.dict(os.environ, {"SAGE_CHAT_FILE": ":memory:"}):
            self.assertEqual(runtime_path("SAGE_CHAT_FILE", "default"), ":memory:")

    def test_legacy_examguard_runtime_directory_migrates_once(self):
        from algosage.runtime_paths import runtime_path

        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / ".examguard"
            legacy.mkdir()
            (legacy / "chats.sqlite3").write_bytes(b"legacy")
            with patch.dict(
                os.environ,
                {
                    "CHAINLIT_APP_ROOT": root,
                    "SAGE_CHAT_FILE": ".algosage/chats.sqlite3",
                },
            ):
                migrated = Path(
                    runtime_path("SAGE_CHAT_FILE", ".algosage/chats.sqlite3")
                )
                self.assertEqual(migrated.read_bytes(), b"legacy")
                self.assertTrue(migrated.exists())
                self.assertFalse(legacy.exists())

    def test_sqlite_runtime_directories_and_files_are_private(self):
        import asyncio

        from algosage.chat_store import ChatDataLayer
        from algosage.state_store import RiskStateStore
        from algosage.student_auth import StudentAuthStore

        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root) / ".algosage"
            chat_path = runtime / "chats.sqlite3"
            auth_path = runtime / "auth.sqlite3"
            state_path = runtime / "state.sqlite3"

            async def exercise_chat() -> None:
                store = ChatDataLayer(str(chat_path))
                await store.create_user(
                    type(
                        "User",
                        (),
                        {
                            "identifier": "alice",
                            "display_name": "Alice",
                            "metadata": {},
                        },
                    )()
                )
                await store.close()

            asyncio.run(exercise_chat())
            StudentAuthStore(str(auth_path)).close()
            state_store = RiskStateStore(str(state_path))
            budget = state_store.get("personal", "student:alice")
            with state_store.turn(budget):
                pass
            lock_file = next((runtime / ".state-locks").glob("*.lock"))
            self.assertEqual(lock_file.stat().st_mode & 0o777, 0o600)

            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual((runtime / "elements").stat().st_mode & 0o777, 0o700)
            self.assertEqual((runtime / ".state-locks").stat().st_mode & 0o777, 0o700)
            for path in (chat_path, auth_path, state_path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(path) + suffix)
                    if sidecar.exists():
                        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600, sidecar)

    def test_generated_auth_secret_directory_is_private(self):
        from algosage.local_identity import ensure_local_secret

        with tempfile.TemporaryDirectory() as root:
            secret_path = Path(root) / ".algosage" / "auth.secret"
            stale_dir = secret_path.parent / "backups"
            stale_dir.mkdir(parents=True)
            stale_file = stale_dir / "old.sqlite3"
            stale_file.write_bytes(b"old")
            stale_dir.chmod(0o755)
            stale_file.chmod(0o644)
            with patch.dict(
                os.environ,
                {
                    "CHAINLIT_APP_ROOT": root,
                    "SAGE_AUTH_SECRET_FILE": str(secret_path),
                    "CHAINLIT_AUTH_SECRET": "",
                },
            ):
                ensure_local_secret()
            self.assertEqual(secret_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stale_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(stale_file.stat().st_mode & 0o777, 0o600)

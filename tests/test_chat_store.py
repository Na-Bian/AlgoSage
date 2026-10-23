import asyncio
import json

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from chainlit.types import Pagination, ThreadFilter
from chainlit.types import Feedback
from chainlit.user import User

from algosage.chat_store import ChatDataLayer


def run(coro):
    return asyncio.run(coro)


def step(step_id, thread_id, output, created_at):
    return {
        "id": step_id,
        "threadId": thread_id,
        "type": "assistant_message",
        "name": "answer",
        "parentId": None,
        "output": output,
        "createdAt": created_at,
        "metadata": {
            "provider_selection": "safe-model",
            "secret": "must-not-persist",
        },
        "generation": {"prompt": "private draft", "api_key": "secret"},
        "input": "question",
    }


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_conversations_and_steps_survive_restart(self):
        tmp_path = self.root
        path = str(tmp_path / "chat.sqlite3")
        first = ChatDataLayer(path)
        run(first.create_user(User(identifier="alice", display_name="Alice")))
        run(first.create_step(step("s1", "t1", "first", "2026-01-01T00:00:02Z")))
        run(first.update_thread("t1", name="Exam 1", user_id="alice"))
        run(first.create_step(step("s2", "t1", "second", "2026-01-01T00:00:01Z")))
        run(first.close())

        second = ChatDataLayer(path)
        thread = run(second.get_thread("t1"))
        assert thread["name"] == "Exam 1"
        assert [item["id"] for item in thread["steps"]] == ["s2", "s1"]
        assert "metadata" not in thread["steps"][0]
        assert "generation" not in thread["steps"][0]
        assert "secret" not in json.dumps(thread)
        run(second.close())

    def test_list_is_scoped_searchable_and_cursor_paginated(self):
        tmp_path = self.root
        store = ChatDataLayer(str(tmp_path / "chat.sqlite3"))
        for user_id, thread_id, name in [
            ("alice", "t1", "Alpha"),
            ("alice", "t2", "Beta"),
            ("bob", "t3", "Alpha other"),
        ]:
            run(store.update_thread(thread_id, name=name, user_id=user_id))

        response = run(
            store.list_threads(Pagination(first=1), ThreadFilter(userId="alice"))
        )
        assert [thread["id"] for thread in response.data] == ["t2"]
        assert response.pageInfo.hasNextPage is True
        next_page = run(
            store.list_threads(
                Pagination(first=1, cursor=response.pageInfo.endCursor),
                ThreadFilter(userId="alice"),
            )
        )
        assert [thread["id"] for thread in next_page.data] == ["t1"]
        assert (
            run(
                store.list_threads(Pagination(first=100), ThreadFilter(userId="bob"))
            ).data[0]["id"]
            == "t3"
        )
        assert (
            run(
                store.list_threads(
                    Pagination(first=100), ThreadFilter(userId=None, search="Alpha")
                )
            ).data
            == []
        )
        assert (
            run(
                store.list_threads(
                    Pagination(first=100), ThreadFilter(userId="alice", search="alpha")
                )
            ).data[0]["id"]
            == "t1"
        )
        run(store.close())

    def test_owner_cannot_be_rebound_and_delete_keeps_independent_risk_db(self):
        tmp_path = self.root
        store = ChatDataLayer(str(tmp_path / "chat.sqlite3"))
        run(store.update_thread("t1", name="Owned", user_id="alice"))
        with self.assertRaises(PermissionError):
            run(store.update_thread("t1", user_id="bob"))
        assert run(store.get_thread_author("t1")) == "alice"

        import sqlite3

        with sqlite3.connect(str(tmp_path / "chat.sqlite3")) as conn:
            conn.execute(
                "CREATE TABLE risk_budget (id TEXT PRIMARY KEY, value INTEGER)"
            )
            conn.execute("INSERT INTO risk_budget VALUES ('alice', 7)")
            conn.commit()
        run(store.delete_thread("t1"))
        with sqlite3.connect(str(tmp_path / "chat.sqlite3")) as conn:
            assert conn.execute(
                "SELECT value FROM risk_budget WHERE id='alice'"
            ).fetchone() == (7,)
        run(store.close())

    def test_thread_metadata_whitelist_and_name_bound(self):
        tmp_path = self.root
        store = ChatDataLayer(str(tmp_path / "chat.sqlite3"))
        run(
            store.update_thread(
                "t1",
                name="x" * 1000,
                user_id="alice",
                metadata={
                    "chat_settings": {
                        "provider_selection": "provider/model",
                        "exam_mode_switch": True,
                        "private": "drop",
                    },
                    "draft": "drop",
                },
            )
        )
        thread = run(store.get_thread("t1"))
        assert len(thread["name"]) <= 200
        assert thread["metadata"] == {
            "chat_settings": {
                "provider_selection": "provider/model",
                "exam_mode_switch": True,
            }
        }
        run(store.close())

    def test_backend_history_is_owner_scoped_and_survives_restart(self):
        tmp_path = self.root
        path = str(tmp_path / "chat.sqlite3")
        store = ChatDataLayer(path)
        run(store.create_user(User(identifier="alice")))
        run(store.update_thread("t1", user_id="alice"))
        run(
            store.save_backend_history(
                "t1",
                "alice",
                [
                    {"role": "user", "content": "question", "draft": "drop"},
                    {"role": "assistant", "content": "answer"},
                ]
                * 20,
                "provider/model",
            )
        )
        with self.assertRaises(PermissionError):
            run(store.load_backend_history("t1", "bob"))
        run(store.close())

        reopened = ChatDataLayer(path)
        history, provider = run(reopened.load_backend_history("t1", "alice"))
        assert len(history) == 24
        assert all(set(message) <= {"role", "content"} for message in history)
        assert provider == "provider/model"
        assert run(reopened.get_thread("t1"))["metadata"] == {
            "chat_settings": {"provider_selection": "provider/model"}
        }
        run(reopened.close())

    def test_deleted_thread_rejects_late_backend_history_write(self):
        tmp_path = self.root
        store = ChatDataLayer(str(tmp_path / "chat.sqlite3"))
        run(store.create_user(User(identifier="alice")))
        run(store.update_thread("t1", user_id="alice"))
        run(store.delete_thread("t1"))
        with self.assertRaises(PermissionError):
            run(store.save_backend_history("t1", "alice", [], "provider/model"))
        assert run(store.get_thread("t1")) is None
        run(store.close())

    def test_account_closure_deletes_only_owned_personal_data(self):
        tmp_path = self.root
        store = ChatDataLayer(str(tmp_path / "chat.sqlite3"))
        run(store.create_user(User(identifier="alice", display_name="Alice")))
        run(store.create_user(User(identifier="bob", display_name="Bob")))
        for owner, thread_id in [("alice", "alice-1"), ("alice", "alice-2"), ("bob", "bob-1")]:
            run(store.update_thread(thread_id, name=f"{owner} history", user_id=owner))
            run(store.create_step(step(f"{thread_id}-step", thread_id, "answer", "2026-01-01T00:00:00Z")))

        alice_element = tmp_path / "alice-attachment.txt"
        alice_element.write_text("alice private attachment", encoding="utf-8")
        element = SimpleNamespace(
            id="alice-element", thread_id="alice-1", for_id="alice-1-step",
            type="file", name="alice-attachment.txt", mime="text/plain",
            display="inline", size="small", language=None, path=str(alice_element),
            url=None, object_key=None, chainlit_key="alice-upload",
        )
        run(store.create_element(element))
        persisted_attachment = Path(store.get_element_path("alice-1", "alice-element"))
        self.assertEqual(persisted_attachment.stat().st_mode & 0o777, 0o600)
        self.assertEqual(persisted_attachment.parent.stat().st_mode & 0o777, 0o700)

        deleted = run(store.delete_all_personal_data("alice"))
        self.assertEqual(deleted, 2)
        self.assertIsNone(run(store.get_thread("alice-1")))
        self.assertIsNone(run(store.get_thread("alice-2")))
        self.assertEqual(run(store.get_thread("bob-1"))["name"], "bob history")
        self.assertFalse(persisted_attachment.exists())
        self.assertIsNone(run(store.get_user("alice")))
        self.assertEqual(run(store.get_user("bob")).identifier, "bob")
        run(store.close())

    def test_step_uuid_cannot_move_between_threads(self):
        store = ChatDataLayer(str(self.root / "chat.sqlite3"))
        run(store.update_thread("victim", user_id="alice"))
        run(store.update_thread("attacker", user_id="bob"))
        run(store.create_step(step("shared-id", "victim", "original", "2026-01-01")))
        with self.assertRaises(PermissionError):
            run(
                store.create_step(
                    step("shared-id", "attacker", "tampered", "2026-01-02")
                )
            )
        assert run(store.get_thread("victim"))["steps"][0]["output"] == "original"
        assert run(store.get_thread("attacker"))["steps"] == []
        run(store.close())

    def test_uploaded_element_and_feedback_survive_restart(self):
        path = self.root / "chat.sqlite3"
        source = self.root / "notes.pdf"
        source.write_bytes(b"PDF fixture for persisted history")
        store = ChatDataLayer(str(path))
        run(store.create_user(User(identifier="alice")))
        run(store.update_thread("t1", name="History", user_id="alice"))
        run(store.create_step(step("s1", "t1", "answer", "2026-01-01T00:00:00Z")))
        element = SimpleNamespace(
            id="e1", thread_id="t1", for_id="s1", type="file", name="notes.pdf",
            mime="application/pdf", display="inline", size="medium", language=None,
            path=str(source), url=None, object_key=None, chainlit_key="upload-e1",
        )
        run(store.create_element(element))
        feedback_id = run(
            store.upsert_feedback(
                Feedback(forId="s1", value=1, threadId="t1", comment="有帮助")
            )
        )
        self.assertTrue(feedback_id)
        thread = run(store.get_thread("t1"))
        self.assertEqual(thread["elements"][0]["name"], "notes.pdf")
        self.assertEqual(thread["steps"][0]["feedback"]["value"], 1)
        self.assertIn("/project/element/t1/e1/content", thread["elements"][0]["url"])
        run(store.close())

        reopened = ChatDataLayer(str(path))
        thread = run(reopened.get_thread("t1"))
        self.assertEqual(thread["elements"][0]["mime"], "application/pdf")
        self.assertEqual(thread["steps"][0]["feedback"]["comment"], "有帮助")
        element_dict = run(reopened.get_element("t1", "e1"))
        self.assertEqual(element_dict["id"], "e1")
        self.assertTrue(Path(reopened.get_element_path("t1", "e1")).is_file())
        self.assertTrue(run(reopened.delete_feedback(feedback_id)))
        self.assertIsNone(run(reopened.get_thread("t1"))["steps"][0].get("feedback"))
        run(reopened.delete_thread("t1"))
        self.assertIsNone(reopened.get_element_path("t1", "e1"))
        run(reopened.close())

    def test_feedback_writes_and_deletes_are_owner_scoped(self):
        store = ChatDataLayer(str(self.root / "chat.sqlite3"))
        run(store.update_thread("alice-thread", user_id="alice"))
        run(store.update_thread("bob-thread", user_id="bob"))
        run(store.create_step(step("a1", "alice-thread", "answer", "2026-01-01T00:00:00Z")))

        feedback_id = run(
            store.upsert_feedback_for(
                Feedback(forId="a1", value=1, threadId="alice-thread"),
                "alice",
            )
        )
        thread = run(store.get_thread("alice-thread"))
        self.assertEqual(thread["steps"][0]["feedback"]["value"], 1)

        with self.assertRaises(PermissionError):
            run(
                store.upsert_feedback_for(
                    Feedback(forId="a1", value=0, threadId="alice-thread"),
                    "bob",
                )
            )
        thread = run(store.get_thread("alice-thread"))
        self.assertEqual(thread["steps"][0]["feedback"]["value"], 1)

        with self.assertRaises(PermissionError):
            run(store.delete_feedback_for(feedback_id, "bob"))
        thread = run(store.get_thread("alice-thread"))
        self.assertIn("feedback", thread["steps"][0])

        self.assertTrue(run(store.delete_feedback_for(feedback_id, "alice")))
        self.assertFalse(run(store.delete_feedback_for("missing", "alice")))
        thread = run(store.get_thread("alice-thread"))
        self.assertNotIn("feedback", thread["steps"][0])
        run(store.close())

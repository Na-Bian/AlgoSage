"""User-facing stream is always the service's final audited response."""

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import chainlit_app


class ChatFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_stream_contains_only_final_response(self):
        self.assertTrue(
            hasattr(chainlit_app, "deliver_response"),
            "approved streaming delivery missing",
        )
        reply = SimpleNamespace(content="", stream_token=AsyncMock(), send=AsyncMock())
        answer = "已审核的安全解释。" * 12
        await chainlit_app.deliver_response(reply, answer)
        chunks = [c.args[0] for c in reply.stream_token.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), answer)
        reply.send.assert_awaited_once()

    async def test_message_error_never_streams_unreviewed_draft(self):
        self.assertTrue(
            hasattr(chainlit_app, "deliver_response"),
            "approved streaming delivery missing",
        )
        from algosage.service import TutorService
        from algosage.config import Settings

        svc = TutorService(Settings())
        reply = SimpleNamespace(content="", stream_token=AsyncMock(), send=AsyncMock())
        fake = SimpleNamespace(
            context=SimpleNamespace(session=SimpleNamespace(id="stream-error")),
            user_session=SimpleNamespace(get=lambda key: None),
            Message=Mock(return_value=reply),
            Step=Mock(),
        )
        # Real callback catches a service error before delivery starts.
        with patch.object(chainlit_app, "get_service", return_value=svc), patch.object(
            chainlit_app, "cl", fake
        ), patch.object(
            chainlit_app, "run_turn", side_effect=RuntimeError("PRIVATE DRAFT")
        ):
            await inspect.unwrap(chainlit_app.on_message)(
                SimpleNamespace(content="解释前缀", id="backend-error-test")
            )
        self.assertIn("RuntimeError", reply.content)
        self.assertNotIn("PRIVATE DRAFT", reply.content)
        reply.stream_token.assert_not_awaited()

    def test_resume_callback_registered(self):
        self.assertTrue(
            callable(getattr(chainlit_app, "on_chat_resume", None)),
            "chat resume missing",
        )

    async def test_restoring_while_other_turn_running_does_not_block_event_loop(self):
        import threading
        import time
        from algosage.service import TutorService
        from algosage.config import Settings

        svc = TutorService(Settings())
        ctx = svc.new_context("resume-lock", principal_id="p")
        held = threading.Event()

        def hold_lock():
            with ctx._budget.lock:
                held.set()
                time.sleep(0.15)

        worker = asyncio.create_task(asyncio.to_thread(hold_lock))
        await asyncio.to_thread(held.wait)
        ticks = []

        async def heartbeat():
            await asyncio.sleep(0.02)
            ticks.append(time.monotonic())

        started = time.monotonic()
        await asyncio.gather(
            asyncio.to_thread(chainlit_app._restore_context_history, ctx, []),
            heartbeat(),
        )
        await worker
        self.assertLess(ticks[0] - started, 0.1)

    async def test_reconnect_reloads_private_history_and_provider(self):
        self.assertTrue(
            hasattr(chainlit_app, "restore_conversation"),
            "authoritative thread history restore missing",
        )
        from algosage.config import Settings
        from algosage.providers import ProviderRegistry
        from algosage.service import TutorService
        from algosage.chat_store import ChatDataLayer
        from chainlit.user import User

        store = ChatDataLayer(":memory:")
        await store.create_user(User(identifier="p"))
        registry = ProviderRegistry(
            Settings(), entries=[{"id": "other", "mode": "mock", "models": ["m"]}]
        )
        svc = TutorService(registry.base, providers=registry)
        fake = SimpleNamespace(
            context=SimpleNamespace(
                session=SimpleNamespace(id="socket-new", thread_id="thread")
            ),
            user_session=SimpleNamespace(
                get=lambda key: (
                    SimpleNamespace(identifier="p") if key == "user" else None
                )
            ),
        )
        history = [
            {"role": "user", "content": "before"},
            {"role": "assistant", "content": "safe"},
        ]
        await store.save_backend_history("thread", "p", history, "other::m")
        with patch.object(chainlit_app, "cl", fake), patch.object(
            chainlit_app, "get_chat_store", return_value=store
        ):
            ctx = await chainlit_app.restore_conversation(svc)
        self.assertEqual(ctx.history, history)
        self.assertEqual(ctx.provider_selection, "other::m")
        self.assertEqual(ctx.session_id, "thread")
        await store.close()

    async def test_settings_from_stale_window_cannot_overwrite_completed_turn(self):
        self.assertTrue(
            hasattr(chainlit_app, "restore_conversation"),
            "authoritative thread history restore missing",
        )
        from algosage.config import Settings
        from algosage.providers import ProviderRegistry
        from algosage.service import TutorService
        from algosage.chat_store import ChatDataLayer
        from chainlit.user import User

        store = ChatDataLayer(":memory:")
        await store.create_user(User(identifier="p"))
        registry = ProviderRegistry(
            Settings(), entries=[{"id": "other", "mode": "mock", "models": ["m"]}]
        )
        svc = TutorService(registry.base, providers=registry)
        history = [
            {"role": "user", "content": "latest turn"},
            {"role": "assistant", "content": "safe"},
        ]
        await store.save_backend_history("thread", "p", history, registry.default)
        stale = svc.new_context("thread", principal_id="p")
        self.assertEqual(stale.history, [])
        fake = SimpleNamespace(
            context=SimpleNamespace(
                session=SimpleNamespace(id="socket", thread_id="thread")
            ),
            user_session=SimpleNamespace(
                get=lambda key: (
                    SimpleNamespace(identifier="p") if key == "user" else None
                )
            ),
            Message=Mock(return_value=SimpleNamespace(send=AsyncMock())),
        )
        with patch.object(chainlit_app, "cl", fake), patch.object(
            chainlit_app, "get_service", return_value=svc
        ), patch.object(chainlit_app, "get_chat_store", return_value=store):
            await inspect.unwrap(chainlit_app.on_settings_update)(
                {"provider_selection": "other::m"}
            )
        saved, selection = await store.load_backend_history("thread", "p")
        self.assertEqual(saved, history)
        self.assertEqual(selection, "other::m")
        await store.close()

    async def test_two_windows_append_turns_to_latest_private_history(self):
        from algosage.config import Settings
        from algosage.service import TutorService
        from algosage.schemas import LLMResult
        from algosage.chat_store import ChatDataLayer
        from chainlit.user import User

        class Backend:
            def __init__(self):
                self.messages = []

            def generate(self, messages):
                self.messages.append(messages)
                return LLMResult("前缀表示从首字符开始的连续片段。", "recording")

        class Step:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        store = ChatDataLayer(":memory:")
        await store.create_user(User(identifier="p"))
        backend = Backend()
        svc = TutorService(Settings(), backend=backend)
        fake = SimpleNamespace(
            context=SimpleNamespace(
                session=SimpleNamespace(id="socket", thread_id="thread")
            ),
            user_session=SimpleNamespace(
                get=lambda key: (
                    SimpleNamespace(identifier="p") if key == "user" else None
                )
            ),
            Step=Mock(side_effect=lambda **kwargs: Step()),
        )
        with patch.object(chainlit_app, "cl", fake), patch.object(
            chainlit_app, "get_chat_store", return_value=store
        ):
            await asyncio.gather(
                chainlit_app.run_turn(svc, None, "解释前缀", ""),
                chainlit_app.run_turn(svc, None, "解释后缀", ""),
            )
        history, _ = await store.load_backend_history("thread", "p")
        self.assertEqual(len(history), 4)
        self.assertTrue(any(m["content"] == "解释前缀" for m in backend.messages[-1]))
        self.assertTrue(any(m["role"] == "assistant" for m in backend.messages[-1]))
        await store.close()

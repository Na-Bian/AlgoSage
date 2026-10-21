"""Chainlit 页面与后端集成测试（需要安装 chainlit；未安装时自动跳过）。

说明：Chainlit 的回调依赖运行中的 socket 服务器，无法在单测里直接驱动
on_message；因此这里验证的是页面模块的“可导入 + 纯函数行为 + 服务装配”：
- chainlit_app / eval_page 可在无服务器环境导入（装饰器只注册回调）；
- 输入拆分（围栏代码块）、审计元数据渲染等纯函数正确；
- 页面使用的 TutorService 与后端链路真实连通（mock 后端）。
"""

import unittest
import asyncio
import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import chainlit  # noqa: F401

    HAS_CHAINLIT = True
except ImportError:  # pragma: no cover - 依赖缺失时跳过
    HAS_CHAINLIT = False


def _isolated_env():
    """让页面服务在测试中固定使用 mock 模式，不受本地 .env 影响。"""
    return mock.patch.dict(
        "os.environ",
        {
            "APP_MODE": "mock",
            "EXAM_MODE": "true",
            "SAGE_ALLOW_POLICY_SWITCH": "false",
            "SAGE_STATE_FILE": "",
        },
    )


@unittest.skipUnless(HAS_CHAINLIT, "chainlit 未安装")
class ChainlitAppModuleTests(unittest.TestCase):
    def setUp(self):
        import chainlit_app

        self.module = chainlit_app
        from algosage.chat_store import ChatDataLayer

        store = ChatDataLayer(":memory:")
        self.module._chat_store = store
        self.addCleanup(lambda: asyncio.run(store.close()))
        self.addCleanup(setattr, self.module, "_chat_store", None)
        env = _isolated_env()
        env.start()
        self.addCleanup(env.stop)
        self.module._service = None  # 重置惰性单例，保证环境隔离
        self.addCleanup(setattr, self.module, "_service", None)

    def test_forged_reset_action_is_denied_without_erasing_risk(self):
        service = self.module.get_service()
        ctx = service.new_context("forged-reset")
        service.chat(ctx, "解释什么是 KMP 前缀函数", question_id="q")
        before = ctx.session_guard.snapshot()
        message = mock.Mock()
        message.send = mock.AsyncMock()
        fake_cl = SimpleNamespace(
            context=SimpleNamespace(session=SimpleNamespace(id=ctx.session_id)),
            Message=mock.Mock(return_value=message),
            user_session=SimpleNamespace(get=lambda key: None),
        )
        with mock.patch.object(self.module, "cl", fake_cl):
            asyncio.run(inspect.unwrap(self.module.on_reset_session)(mock.Mock()))
        self.assertEqual(
            service.get_context(ctx.session_id).session_guard.snapshot(), before
        )
        self.assertIn("权限", fake_cl.Message.call_args.kwargs["content"])

    def test_chat_start_binds_authenticated_identifier(self):
        svc = self.module.get_service()
        sent = mock.Mock(send=mock.AsyncMock())
        fake_cl = SimpleNamespace(
            context=SimpleNamespace(session=SimpleNamespace(id="authenticated-start")),
            user_session=SimpleNamespace(
                get=lambda key: (
                    SimpleNamespace(identifier="trusted-student")
                    if key == "user"
                    else None
                )
            ),
            Message=mock.Mock(return_value=sent),
            ChatSettings=mock.Mock(return_value=sent),
        )
        with mock.patch.object(self.module, "cl", fake_cl):
            asyncio.run(inspect.unwrap(self.module.on_chat_start)())
        fake_cl.Message.assert_not_called()
        ctx = svc.get_context("authenticated-start")
        self.assertEqual(ctx.principal_id, "trusted-student")
        self.assertIs(
            ctx.session_guard,
            svc.new_context(
                "another-chat", principal_id="trusted-student"
            ).session_guard,
        )

    def test_feedback_http_routes_are_owner_scoped(self):
        from fastapi.testclient import TestClient
        from chainlit.server import app, get_current_user

        store = self.module._chat_store
        asyncio.run(store.update_thread("alice-thread", user_id="alice"))
        asyncio.run(store.update_thread("bob-thread", user_id="bob"))
        asyncio.run(
            store.create_step(
                {
                    "id": "answer-1",
                    "threadId": "alice-thread",
                    "type": "assistant_message",
                    "name": "answer",
                    "output": "answer",
                    "createdAt": "2026-01-01T00:00:00Z",
                }
            )
        )

        with TestClient(app) as client:
            self.addCleanup(app.dependency_overrides.clear)
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                identifier="bob"
            )
            denied = client.put(
                "/feedback",
                json={
                    "feedback": {
                        "forId": "answer-1",
                        "value": 1,
                        "threadId": "alice-thread",
                    },
                    "sessionId": "unrelated-session",
                },
            )
            self.assertEqual(denied.status_code, 403)
            self.assertIsNone(store.get_thread("alice-thread")["steps"][0].get("feedback"))

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                identifier="alice"
            )
            allowed = client.put(
                "/feedback",
                json={
                    "feedback": {
                        "forId": "answer-1",
                        "value": 1,
                        "threadId": "alice-thread",
                        "comment": "helpful",
                    },
                    "sessionId": "unrelated-session",
                },
            )
            self.assertEqual(allowed.status_code, 200)
            feedback_id = allowed.json()["feedbackId"]
            self.assertEqual(
                store.get_thread("alice-thread")["steps"][0]["feedback"]["comment"],
                "helpful",
            )

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                identifier="bob"
            )
            denied_delete = client.delete(
                "/feedback", json={"feedbackId": feedback_id}
            )
            self.assertEqual(denied_delete.status_code, 403)
            self.assertIn("feedback", store.get_thread("alice-thread")["steps"][0])

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                identifier="alice"
            )
            allowed_delete = client.delete(
                "/feedback", json={"feedbackId": feedback_id}
            )
            self.assertEqual(allowed_delete.status_code, 200)
            self.assertNotIn(
                "feedback", store.get_thread("alice-thread")["steps"][0]
            )

    def test_bootstrap_welcome_uses_public_runtime_settings(self):
        from algosage.config import Settings
        from algosage.service import TutorService

        service = TutorService(
            Settings(
                mode="mock",
                model="display-model",
                exam_mode=False,
                api_key="private-key-sentinel",
            )
        )
        with mock.patch.object(self.module, "get_service", return_value=service):
            profiles = asyncio.run(inspect.unwrap(self.module.chat_profiles)(None))
        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].default)
        self.assertIn("display-model", profiles[0].markdown_description)
        self.assertIn("练习演示", profiles[0].markdown_description)
        self.assertNotIn("private-key-sentinel", profiles[0].markdown_description)

    def test_teacher_demo_keeps_reset_action_available(self):
        service = self.module.get_service()
        service.allow_policy_override = True
        sent = mock.Mock(send=mock.AsyncMock())
        fake = SimpleNamespace(
            context=SimpleNamespace(session=SimpleNamespace(id="teacher-welcome")),
            user_session=SimpleNamespace(get=lambda key: None),
            Message=mock.Mock(return_value=sent),
            Action=mock.Mock(),
            ChatSettings=mock.Mock(return_value=sent),
        )
        with mock.patch.object(self.module, "cl", fake):
            asyncio.run(inspect.unwrap(self.module.on_chat_start)())
        self.assertEqual(len(fake.Message.call_args.kwargs["actions"]), 1)
        self.assertEqual(fake.Action.call_args.kwargs["name"], "reset_session")

    def test_disconnect_preserves_shared_thread_context_and_guard(self):
        svc = self.module.get_service()
        ctx = svc.new_context("disconnect")
        svc.chat(ctx, "解释 KMP 前缀函数", question_id="q")
        before = ctx.session_guard.snapshot()
        fake_cl = SimpleNamespace(
            context=SimpleNamespace(session=SimpleNamespace(id=ctx.session_id))
        )
        with mock.patch.object(self.module, "cl", fake_cl):
            asyncio.run(inspect.unwrap(self.module.on_chat_end)())
        fresh = svc.get_context(ctx.session_id)
        self.assertTrue(fresh.history)
        self.assertEqual(fresh.session_guard.snapshot(), before)

    def test_authenticated_identifier_is_only_identity_source(self):
        for user, expected in (
            (SimpleNamespace(identifier="student"), "student"),
            (None, None),
        ):
            with self.subTest(user=user):
                fake_cl = SimpleNamespace(
                    user_session=SimpleNamespace(
                        get=lambda key: user if key == "user" else None
                    )
                )
                with mock.patch.object(self.module, "cl", fake_cl):
                    self.assertEqual(self.module.authenticated_principal_id(), expected)

    def test_module_registers_required_callbacks(self):
        # 装饰器仅注册回调，不在导入期执行；此处验证入口齐备。
        self.assertTrue(callable(self.module.on_chat_start))
        self.assertTrue(callable(self.module.on_message))
        self.assertTrue(callable(self.module.on_chat_end))
        self.assertTrue(callable(self.module.on_reset_session))

    def test_get_service_builds_singleton(self):
        service_a = self.module.get_service()
        service_b = self.module.get_service()
        self.assertIs(service_a, service_b)
        self.assertEqual(service_a.settings.mode, "mock")
        self.assertFalse(service_a.allow_policy_override)

    def test_split_question_and_code_extracts_fence(self):
        message = "这段 KMP 为什么出错？\n```python\ndef kmp():\n    pass\n```"
        question, code = self.module.split_question_and_code(message)
        self.assertEqual(question, "这段 KMP 为什么出错？")
        self.assertIn("def kmp():", code)

    def test_split_plain_question_keeps_text(self):
        question, code = self.module.split_question_and_code("什么是前缀函数？")
        self.assertEqual(question, "什么是前缀函数？")
        self.assertEqual(code, "")

    def test_split_code_only_uses_neutral_default(self):
        question, code = self.module.split_question_and_code("```python\nprint(1)\n```")
        self.assertTrue(question)
        self.assertIn("print(1)", code)

    def test_render_audit_markdown_brief_is_one_line(self):
        meta = {
            "intent": "hint",
            "help_level": 2,
            "approved": False,
            "audit_reason": "code_block_over_limit",
            "session_risk": 0.45,
        }
        rendered = self.module.render_audit_markdown(meta)
        self.assertIn("`hint`", rendered)
        self.assertIn("L2", rendered)
        self.assertIn("code_block_over_limit", rendered)
        self.assertIn("0.45", rendered)
        # brief 模式不展示耗时/Token/分类方法等细节
        self.assertNotIn("ms", rendered)
        self.assertNotIn("api_key", rendered)

    def test_render_audit_markdown_full_mode_shows_details(self):
        meta = {
            "intent": "hint",
            "classify_method": "regex-v0",
            "classify_reason": "检测到方向性提示需求",
            "help_level": 2,
            "policy_reason": "考试策略：最高帮助等级 2",
            "approved": False,
            "audit_reason": "code_block_over_limit",
            "model": "mock-offline",
            "input_tokens": 12,
            "output_tokens": 34,
        }
        with mock.patch.dict("os.environ", {"SAGE_UI_DETAIL": "full"}):
            rendered = self.module.render_audit_markdown(meta, latency_ms=87)
        self.assertIn("87 ms", rendered)
        self.assertIn("12 / 34", rendered)
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("系统提示词", rendered)

    def test_service_chat_via_page_service_mock_flow(self):
        from algosage.service import TutorService

        service = self.module.get_service()
        self.assertIsInstance(service, TutorService)
        context = service.new_context("unit-chainlit-flow")
        response = service.chat(context, "解释什么是哈夫曼编码")
        self.assertTrue(response.answer)
        meta = TutorService.display_metadata(response)
        self.assertEqual(meta["intent"], "knowledge")


@unittest.skipUnless(HAS_CHAINLIT, "chainlit 未安装")
class EvalPageModuleTests(unittest.TestCase):
    def setUp(self):
        import eval_page

        self.module = eval_page
        env = _isolated_env()
        env.start()
        self.addCleanup(env.stop)
        self.module._service = None
        self.addCleanup(setattr, self.module, "_service", None)

    def test_dataset_paths_cannot_escape_root_or_follow_outside_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "datasets"
            root.mkdir()
            outside = Path(tmp) / "private.jsonl"
            outside.write_text('{"question":"secret"}\n')
            (root / "linked.jsonl").symlink_to(outside)
            with mock.patch.object(self.module, "DATASET_DIR", root):
                for name in ("../private.jsonl", str(outside), "linked.jsonl"):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        self.module.resolve_dataset_path(name)

    def test_formal_harness_schema_is_explicitly_rejected(self):
        svc = self.module.get_service()
        with self.assertRaisesRegex(ValueError, "Harness"):
            self.module.run_cases(
                svc,
                [
                    {
                        "case_id": "formal",
                        "prompt": "解释 KMP",
                        "intent_gold": "knowledge",
                    }
                ],
            )
        with self.assertRaisesRegex(ValueError, "Harness"):
            self.module.run_cases(
                svc, [{"case_id": "multi", "turns": [{"prompt": "解释 KMP"}]}]
            )

    def test_eval_runs_through_service_chat_and_cleans_context(self):
        svc = self.module.get_service()
        with mock.patch.object(svc, "chat", wraps=svc.chat) as chat:
            self.module.run_cases(svc, self.module.load_cases("smoke"))
        self.assertEqual(chat.call_count, 5)
        self.assertEqual(len(svc._contexts), 0)

    def test_load_cases_reads_smoke_dataset(self):
        cases = self.module.load_cases("smoke")
        self.assertEqual(len(cases), 5)
        self.assertIn("question", cases[0])
        self.assertIn("expected_intent", cases[0])

    def test_load_cases_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.module.load_cases("no-such-dataset")

    def test_run_cases_mock_backend_full_accuracy(self):
        from algosage.config import Settings
        from algosage.llm import MockChatBackend

        service = self.module.TutorService(
            Settings(mode="mock", exam_mode=True), backend=MockChatBackend()
        )
        summary = self.module.run_cases(service, self.module.load_cases("smoke"))
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["correct"], 5)
        self.assertEqual(summary["accuracy"], 1.0)
        # attack-01 在考试模式下走 policy-only，其余触达 mock 后端
        models = {r["model"] for r in summary["results"]}
        self.assertEqual(models, {"mock-offline", "policy-only"})

    def test_render_summary_contains_table_and_disclaimer(self):
        service = self.module.get_service()
        summary = self.module.run_cases(service, self.module.load_cases("smoke"))
        text = self.module.render_summary(summary, "smoke")
        self.assertIn("| 用例 |", text)
        self.assertIn("不是安全评测结论", text)


if __name__ == "__main__":
    unittest.main()

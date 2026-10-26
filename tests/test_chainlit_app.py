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

    def test_personal_app_does_not_register_teacher_reset(self):
        self.assertFalse(hasattr(self.module, "on_reset_session"))

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

        # 不用 `with TestClient(app)`：Chainlit 的 lifespan shutdown 以
        # os._exit(0) 结束进程（chainlit/server.py），会静默杀掉整个测试
        # 运行器、后续用例全部丢失。裸 TestClient 不触发 lifespan，路由与
        # dependency_overrides 照常工作（与 test_student_auth 同一模式）。
        client = TestClient(app)
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
        self.assertIsNone(
            asyncio.run(store.get_thread("alice-thread"))["steps"][0].get("feedback")
        )

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
            asyncio.run(store.get_thread("alice-thread"))["steps"][0]["feedback"][
                "comment"
            ],
            "helpful",
        )

        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            identifier="bob"
        )
        denied_delete = client.request(
            "DELETE", "/feedback", json={"feedbackId": feedback_id}
        )
        self.assertEqual(denied_delete.status_code, 403)
        self.assertIn(
            "feedback", asyncio.run(store.get_thread("alice-thread"))["steps"][0]
        )

        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            identifier="alice"
        )
        allowed_delete = client.request(
            "DELETE", "/feedback", json={"feedbackId": feedback_id}
        )
        self.assertEqual(allowed_delete.status_code, 200)
        self.assertNotIn(
            "feedback", asyncio.run(store.get_thread("alice-thread"))["steps"][0]
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
        self.assertIn("宽松演示", profiles[0].markdown_description)
        self.assertNotIn("private-key-sentinel", profiles[0].markdown_description)

    def test_personal_app_does_not_expose_policy_switch(self):
        service = self.module.get_service()
        sent = mock.Mock(send=mock.AsyncMock())
        fake_cl = SimpleNamespace(
            ChatSettings=mock.Mock(return_value=sent),
        )
        with mock.patch.object(self.module, "cl", fake_cl):
            asyncio.run(
                self.module.send_provider_settings(service.get_context("personal"))
            )
        self.assertFalse(service.allow_policy_override)
        widgets = fake_cl.ChatSettings.call_args.args[0]
        widget_ids = [widget.id for widget in widgets]
        # 无预置 providers.json 且无自定义条目时不渲染"默认模型"控件
        # （前端据此禁用发送并引导添加）。
        self.assertNotIn("model_catalog", widget_ids)
        self.assertIn("temperature", widget_ids)
        # 学生界面不得出现考试/练习模式开关；采样控制与 Temperature 二选一，仅保留后者
        self.assertNotIn("provider_selection", widget_ids)
        self.assertNotIn("exam_mode_switch", widget_ids)
        self.assertNotIn("top_p", widget_ids)

    def _send_settings_with_user_agent(self, user_agent):
        service = self.module.get_service()
        sent = mock.Mock(send=mock.AsyncMock())
        fake_cl = SimpleNamespace(
            ChatSettings=mock.Mock(return_value=sent),
            context=SimpleNamespace(
                session=SimpleNamespace(environ={"HTTP_USER_AGENT": user_agent})
            ),
        )
        with mock.patch.object(self.module, "cl", fake_cl):
            asyncio.run(
                self.module.send_provider_settings(service.get_context("ua-test"))
            )
        widgets = fake_cl.ChatSettings.call_args.args[0]
        return {widget.id: widget for widget in widgets}

    def test_enter_shortcut_setting_adapts_to_client_platform(self):
        # macOS 显示 Command+Enter；移动端隐藏该设置；文案不带"发送"后缀。
        desktop = self._send_settings_with_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
        )
        self.assertIn("enter_behavior", desktop)
        self.assertEqual(
            list(desktop["enter_behavior"].items.keys()), ["Enter", "Ctrl+Enter"]
        )

        mac = self._send_settings_with_user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"
        )
        self.assertEqual(
            list(mac["enter_behavior"].items.keys()), ["Enter", "Command+Enter"]
        )

        mobile = self._send_settings_with_user_agent(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"
        )
        self.assertNotIn("enter_behavior", mobile)

    def test_missing_enter_behavior_inherits_current_preference(self):
        # 移动端面板不发送该键：保存其它设置时必须保留既有偏好，而非重置。
        inherited = self.module._normalize_ui_settings(
            {"temperature": 0.3}, current={"enter_behavior": "ctrl_enter"}
        )
        self.assertEqual(inherited["enter_behavior"], "ctrl_enter")
        invalid = self.module._normalize_ui_settings(
            {"enter_behavior": "evil"}, current={"enter_behavior": "ctrl_enter"}
        )
        self.assertEqual(invalid["enter_behavior"], "enter")

    def test_partial_settings_payload_inherits_current_values(self):
        # 即时保存会提交单控件载荷：缺失键继承现值，显式键正常覆盖。
        current = {
            "temperature": 0.4,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "max_output_tokens": 1024,
            "enter_behavior": "ctrl_enter",
            "autoscroll": False,
            "keep_drafts": True,
            "animations": True,
            "font_size": "large",
            "audit_summary": True,
        }
        partial = self.module._normalize_ui_settings(
            {"temperature": 1.1}, current=current
        )
        self.assertEqual(partial["temperature"], 1.1)
        self.assertEqual(partial["frequency_penalty"], 0.2)
        self.assertEqual(partial["max_output_tokens"], 1024)
        self.assertEqual(partial["enter_behavior"], "ctrl_enter")
        self.assertFalse(partial["autoscroll"])
        self.assertEqual(partial["font_size"], "large")

    def test_preset_provider_visibility_is_user_scoped(self):
        from fastapi.testclient import TestClient
        from chainlit.server import app, get_current_user

        service = self.module.get_service()
        default_id = service.providers.default.split("::", 1)[0]
        removable = sorted(
            {option["id"] for option in service.providers.options()} - {default_id}
        )
        if not removable:
            self.skipTest("registry only has the default provider")
        target = removable[0]

        client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            identifier="provider-manager"
        )

        catalog = client.get("/models/catalog").json()
        group = next(g for g in catalog["providers"] if g["id"] == target)
        self.assertFalse(group["hidden"])
        self.assertEqual(group["source"], "preset")

        hidden = client.post(
            "/models/preset", json={"provider_id": target, "action": "hide"}
        )
        self.assertEqual(hidden.status_code, 200)
        catalog = client.get("/models/catalog").json()
        group = next(g for g in catalog["providers"] if g["id"] == target)
        self.assertTrue(group["hidden"])

        # 默认 Provider 与未知 id 都要拒绝。
        self.assertEqual(
            client.post(
                "/models/preset", json={"provider_id": default_id, "action": "hide"}
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/models/preset", json={"provider_id": "ghost", "action": "hide"}
            ).status_code,
            404,
        )

        restored = client.post(
            "/models/preset", json={"provider_id": target, "action": "restore"}
        )
        self.assertEqual(restored.status_code, 200)
        catalog = client.get("/models/catalog").json()
        group = next(g for g in catalog["providers"] if g["id"] == target)
        self.assertFalse(group["hidden"])

        # 隐藏状态是账号级偏好，不泄漏给其他用户。
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            identifier="provider-bystander"
        )
        other = client.get("/models/catalog").json()
        other_group = next(g for g in other["providers"] if g["id"] == target)
        self.assertFalse(other_group["hidden"])

    def test_preset_provider_delete_purges_from_catalog(self):
        from fastapi.testclient import TestClient
        from chainlit.server import app, get_current_user

        service = self.module.get_service()
        default_id = service.providers.default.split("::", 1)[0]
        removable = sorted(
            {option["id"] for option in service.providers.options()} - {default_id}
        )
        if not removable:
            self.skipTest("registry only has the default provider")
        target = removable[0]

        client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            identifier="provider-purger"
        )

        purged = client.post(
            "/models/preset", json={"provider_id": target, "action": "delete"}
        )
        self.assertEqual(purged.status_code, 200)
        catalog = client.get("/models/catalog").json()
        self.assertFalse(
            any(g["id"] == target for g in catalog["providers"]),
            "彻底删除的预置提供商不应再出现在目录里",
        )
        # 恢复仍可撤销彻底删除。
        restored = client.post(
            "/models/preset", json={"provider_id": target, "action": "restore"}
        )
        self.assertEqual(restored.status_code, 200)
        catalog = client.get("/models/catalog").json()
        group = next(g for g in catalog["providers"] if g["id"] == target)
        self.assertFalse(group["hidden"])

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
        self.assertFalse(hasattr(self.module, "on_reset_session"))

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

    def test_prepare_message_inputs_rejects_oversized_message_before_model(self):
        with self.assertRaises(ValueError):
            self.module.prepare_message_inputs(
                "x" * (self.module.MAX_MESSAGE_CHARS + 1), ()
            )

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


if __name__ == "__main__":
    unittest.main()

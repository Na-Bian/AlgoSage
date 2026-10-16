"""Chainlit 页面与后端集成测试（需要安装 chainlit；未安装时自动跳过）。

说明：Chainlit 的回调依赖运行中的 socket 服务器，无法在单测里直接驱动
on_message；因此这里验证的是页面模块的“可导入 + 纯函数行为 + 服务装配”：
- chainlit_app / eval_page 可在无服务器环境导入（装饰器只注册回调）；
- 输入拆分（围栏代码块）、审计元数据渲染等纯函数正确；
- 页面使用的 TutorService 与后端链路真实连通（mock 后端）。
"""
import unittest
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
        {"APP_MODE": "mock", "EXAM_MODE": "true", "EG_ALLOW_POLICY_SWITCH": "false"},
    )


@unittest.skipUnless(HAS_CHAINLIT, "chainlit 未安装")
class ChainlitAppModuleTests(unittest.TestCase):
    def setUp(self):
        import chainlit_app

        self.module = chainlit_app
        env = _isolated_env()
        env.start()
        self.addCleanup(env.stop)
        self.module._service = None  # 重置惰性单例，保证环境隔离
        self.addCleanup(setattr, self.module, "_service", None)

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
            "intent": "hint", "help_level": 2, "approved": False,
            "audit_reason": "code_block_over_limit", "session_risk": 0.45,
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
            "intent": "hint", "classify_method": "regex-v0",
            "classify_reason": "检测到方向性提示需求", "help_level": 2,
            "policy_reason": "考试策略：最高帮助等级 2", "approved": False,
            "audit_reason": "code_block_over_limit", "model": "mock-offline",
            "input_tokens": 12, "output_tokens": 34,
        }
        with mock.patch.dict("os.environ", {"EG_UI_DETAIL": "full"}):
            rendered = self.module.render_audit_markdown(meta, latency_ms=87)
        self.assertIn("87 ms", rendered)
        self.assertIn("12 / 34", rendered)
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("系统提示词", rendered)

    def test_service_chat_via_page_service_mock_flow(self):
        from examguard.service import TutorService

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

    def test_load_cases_reads_smoke_dataset(self):
        cases = self.module.load_cases("smoke")
        self.assertEqual(len(cases), 5)
        self.assertIn("question", cases[0])
        self.assertIn("expected_intent", cases[0])

    def test_load_cases_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.module.load_cases("no-such-dataset")

    def test_run_cases_mock_backend_full_accuracy(self):
        from examguard.config import Settings
        from examguard.llm import MockChatBackend

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

import asyncio
import tempfile
import unittest
from pathlib import Path

from algosage.chat_store import ChatDataLayer
from algosage.config import Settings
from algosage.providers import ProviderRegistry
from algosage.schemas import LLMResult


class ChatTitleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "chat.sqlite3")
        self.store = ChatDataLayer(self.path)
        self.addAsyncCleanup(self.store.close)
        await self.store.update_thread(
            "t", name="请解释 KMP 中的前缀函数", user_id="alice"
        )
        self.history = [
            {"role": "user", "content": "请解释 KMP 中的前缀函数", "msg_id": "m1"},
            {"role": "assistant", "content": "已审核的概念解释"},
        ]
        await self.store.save_backend_history("t", "alice", self.history, "default::m")

    async def test_generated_title_is_persisted_searchable_and_claimed_once(self):
        self.assertTrue(await self.store.claim_title("t", "alice"))
        self.assertFalse(await self.store.claim_title("t", "alice"))
        self.assertTrue(await self.store.finish_title("t", "alice", "KMP 前缀函数"))
        second = ChatDataLayer(self.path)
        try:
            self.assertEqual((await second.get_thread("t"))["name"], "KMP 前缀函数")
            from chainlit.types import Pagination, ThreadFilter

            found = await second.list_threads(
                Pagination(first=10), ThreadFilter(userId="alice", search="前缀")
            )
            self.assertEqual([t["name"] for t in found.data], ["KMP 前缀函数"])
            self.assertFalse(await second.claim_title("t", "alice"))
        finally:
            await second.close()

    async def test_manual_rename_wins_over_inflight_generation(self):
        await self.store.claim_title("t", "alice")
        await self.store.update_thread("t", name="我的复习笔记")
        self.assertFalse(await self.store.finish_title("t", "alice", "KMP 前缀函数"))
        self.assertEqual((await self.store.get_thread("t"))["name"], "我的复习笔记")

    async def test_manual_rename_before_claim_disables_generation(self):
        await self.store.update_thread("t", name="我的复习笔记")
        self.assertFalse(await self.store.claim_title("t", "alice"))

    async def test_deleted_thread_cannot_be_resurrected_by_title(self):
        await self.store.claim_title("t", "alice")
        await self.store.delete_thread("t")
        self.assertFalse(await self.store.finish_title("t", "alice", "KMP 前缀函数"))
        self.assertIsNone(await self.store.get_thread("t"))

    async def test_editing_first_question_invalidates_old_title(self):
        await self.store.claim_title("t", "alice")
        self.history[0]["content"] = "请解释最小生成树"
        await self.store.save_backend_history("t", "alice", self.history, "default::m")
        self.assertFalse(await self.store.finish_title("t", "alice", "KMP 前缀函数"))

    async def test_late_framework_initial_name_does_not_overwrite_title(self):
        await self.store.claim_title("t", "alice")
        await self.store.finish_title("t", "alice", "KMP 前缀函数")
        await self.store.update_thread("t", name="原始长问题", user_id="alice")
        self.assertEqual((await self.store.get_thread("t"))["name"], "KMP 前缀函数")

    async def test_wrong_owner_and_later_turn_cannot_claim(self):
        self.assertFalse(await self.store.claim_title("t", "bob"))
        self.history.extend([{"role": "user", "content": "继续", "msg_id": "m2"}])
        await self.store.save_backend_history("t", "alice", self.history, "default::m")
        self.assertFalse(await self.store.claim_title("t", "alice"))

    async def test_failed_generation_keeps_original_and_does_not_retry(self):
        await self.store.claim_title("t", "alice")
        self.assertFalse(await self.store.finish_title("t", "alice", None))
        self.assertEqual(
            (await self.store.get_thread("t"))["name"], "请解释 KMP 中的前缀函数"
        )
        self.assertFalse(await self.store.claim_title("t", "alice"))

    async def test_failed_title_recovers_on_next_turn_same_topic(self):
        """回归：首轮审核被否决/标题生成失败的会话，下一轮同话题重新领取。"""
        await self.store.claim_title("t", "alice")
        self.assertFalse(await self.store.finish_title("t", "alice", None))
        # 同话题再次领取：上次 failed → 允许重试。
        self.assertTrue(
            await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        )
        self.assertTrue(
            await self.store.finish_title(
                "t", "alice", "KMP 前缀函数", "请解释 KMP 中的前缀函数"
            )
        )
        self.assertEqual((await self.store.get_thread("t"))["name"], "KMP 前缀函数")
        # 已命名 + 同话题：不再领取。
        self.assertFalse(
            await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        )

    async def test_titled_thread_never_retitles_on_new_topic(self):
        """会话已命名（status=auto）后永不再生成：追问换话题不改标题
        （用户要求：不要每次都生成新标题）。"""
        await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        await self.store.finish_title(
            "t", "alice", "KMP 前缀函数", "请解释 KMP 中的前缀函数"
        )
        # 同话题 → 拒绝；新话题 → 同样拒绝（保留原标题）。
        self.assertFalse(
            await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        )
        new_q = "编辑后的提问：请解释最小生成树"
        self.assertFalse(await self.store.claim_title("t", "alice", new_q))
        self.assertEqual((await self.store.get_thread("t"))["name"], "KMP 前缀函数")

    async def test_failed_title_can_retry_with_context(self):
        """上次失败（status=failed，无标题）→ 下一次提问时重新领取并生成，
        生成材料可包含会话既有问答（context_text）。"""
        await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        # 模拟模型失败：落名 None → status=failed（无标题）。
        await self.store.finish_title("t", "alice", None, "请解释 KMP 中的前缀函数")
        # 下一次提问重新领取成功。
        self.assertTrue(await self.store.claim_title("t", "alice", "那 BM 算法呢"))
        self.assertTrue(await self.store.finish_title("t", "alice", "KMP 与 BM 匹配算法", "那 BM 算法呢"))
        self.assertEqual((await self.store.get_thread("t"))["name"], "KMP 与 BM 匹配算法")

    async def test_pending_title_blocks_new_claims(self):
        """在途任务（pending）未落名前，追问不叠新任务。"""
        await self.store.claim_title("t", "alice", "请解释 KMP 中的前缀函数")
        self.assertFalse(await self.store.claim_title("t", "alice", "那 BM 算法呢"))

    async def test_framework_name_arriving_after_claim_is_kept_on_failure(self):
        from chainlit.user import User

        await self.store.create_user(User(identifier="alice"))
        await self.store.save_backend_history(
            "new", "alice", self.history, "default::m"
        )
        self.assertTrue(await self.store.claim_title("new", "alice"))
        await self.store.update_thread("new", name="原始问题", user_id="alice")
        await self.store.finish_title("new", "alice", None)
        self.assertEqual((await self.store.get_thread("new"))["name"], "原始问题")


class TitleGenerationTests(unittest.TestCase):
    def generate(self, output, question="请解释 KMP 中的前缀函数", **kwargs):
        from algosage.chat_titles import generate_title
        from algosage.invocation import turn_ledger

        self.calls = 0

        class Backend:
            def generate_with_options(inner, messages, **options):
                self.calls += 1
                self.messages, self.options = messages, options
                if isinstance(output, Exception):
                    raise output
                return LLMResult(output, "controlled", 12, 6)

        # Title usage is separate from, and must not mutate, the tutor ledger.
        with turn_ledger(Settings(mode="mock")) as parent:
            result = generate_title(Backend(), Settings(mode="mock"), question, **kwargs)
        self.assertEqual(parent.calls, [])
        return result

    def test_short_extractive_title_and_bounded_nonstreaming_call(self):
        self.assertEqual(self.generate("KMP 前缀函数"), "KMP 前缀函数")
        self.assertLessEqual(self.options["max_output_tokens"], 96)
        # 小模型偶发慢响应曾把 8 秒预算整个吃掉、标题降级为原文截取；
        # 放宽到 20 秒后仍须有界（远小于回合预算）。
        self.assertLessEqual(self.options["timeout"], 20)

    def test_answer_like_or_unrelated_output_is_not_a_title(self):
        from algosage.chat_titles import valid_title

        for output in (
            "采用动态规划",
            "dp[i] = max(dp[i-1], a[i])",
            "标题\n这是解释",
            "```KMP```",
            "",
            "这是一个非常非常非常非常非常非常非常非常非常非常长的标题",
        ):
            with self.subTest(output=output):
                self.assertIsNone(valid_title(output, "请解释 KMP 中的前缀函数"))

    def test_real_provider_answer_instead_of_title_uses_safe_topic_fallback(self):
        question = "请用两句话解释 KMP 前缀函数的含义，只讲概念，不给代码或求解步骤。"
        answer = "KMP 前缀函数用于计算模式串前缀与自身真前缀的最长匹配长度，帮助优化字符串匹配算法的效率。"
        self.assertEqual(self.generate(answer, question), "KMP 前缀函数")
        self.assertEqual(self.calls, 1)

    def test_naming_task_is_explicit_in_final_message_and_source_is_data(self):
        import json

        self.generate("KMP 前缀函数")
        content = self.messages[-1]["content"]
        data = json.loads(content)
        self.assertEqual(data["text_to_title"], "请解释 KMP 中的前缀函数")
        self.assertIn("标题", data["task"])

    def test_invalid_fallback_never_adds_model_words_or_code(self):
        from algosage.chat_titles import title_source, valid_title

        question = "请介绍广度优先搜索的基本概念。\n```python\nSECRET_CODE = 42\n```"
        title = self.generate("采用动态规划求解", question)
        self.assertIsNotNone(title)
        self.assertEqual(valid_title(title, title_source(question)), title)
        self.assertNotIn("动态规划", title)
        self.assertNotIn("SECRET_CODE", self.messages[-1]["content"])

    def test_failure_is_nonfatal_and_has_no_retry(self):
        self.assertEqual(
            self.generate(TimeoutError("provider unavailable")), "KMP 中的前缀函数"
        )
        self.assertEqual(self.calls, 1)

    def test_source_is_bounded_and_code_is_not_sent(self):
        self.generate(
            "KMP 前缀函数",
            "请解释 KMP 中的前缀函数\n```python\nSECRET_CODE\n```" + "问题" * 1000,
        )
        self.assertNotIn("SECRET_CODE", self.messages[-1]["content"])
        self.assertLess(len(self.messages[-1]["content"]), 700)

    def test_conversation_context_is_included_as_material(self):
        """追问补标题：context_text（会话既有问答）随材料一起给标题模型，
        标题概括整个会话话题。"""
        context = "问：请解释 KMP 中的前缀函数\n答：KMP 前缀函数用于字符串匹配…"
        self.generate(
            "KMP 与 BM 匹配算法",
            "那 BM 算法呢",
            context_text=context,
        )
        data = __import__("json").loads(self.messages[-1]["content"])
        self.assertIn("请解释 KMP", data["text_to_title"])
        self.assertIn("（最新提问）那 BM 算法呢", data["text_to_title"])


class TitleSchedulingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ChatTitleStoreTests.asyncSetUp

    async def test_resumed_conversation_still_gets_titles(self):
        """回归：session.restored（页面刷新/resume 后）曾让标题任务永久跳过
        ——用户在旧线程刷新页面后编辑重发/继续提问，标题再也不更新。现在
        restored 不再是门槛：claim 按话题判断，同话题已命名自然拒绝。"""
        from types import SimpleNamespace
        from unittest.mock import patch

        import chainlit_app

        context = SimpleNamespace(
            session_id="t",
            principal_id="alice",
            provider_selection="default::m",
            history=[],
        )
        service = SimpleNamespace(settings=Settings(mode="mock"))
        with patch.object(chainlit_app, "get_chat_store", return_value=self.store):
            task = await chainlit_app.schedule_title(
                service,
                context,
                "请解释 KMP 中的前缀函数",
                object(),
                SimpleNamespace(thread_id="t", restored=True),
            )
        # 旧线程无标题（setup 未登记 titles 行）→ 任务正常创建。
        self.assertIsNotNone(task)

    async def test_title_job_is_nonblocking_and_refreshes_sidebar(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import chainlit_app
        from algosage.service import TutorService

        # 标题走独立的内置小模型 tutor（不再复用会话 Backend）：测试里直接
        # 替换 service 的标题 tutor，用事件验证任务在后台非阻塞执行。
        entered, release = threading.Event(), threading.Event()

        class TitleBackend:
            def generate_with_options(inner, messages, **options):
                entered.set()
                release.wait(2)
                return LLMResult("KMP 前缀函数", "controlled", 12, 6)

        class SessionBackend:
            # 会话回答照常生成；标题任务是否误用会话模型由「只有 TitleBackend
            # 能置位 entered」这一事实判定（误用时 entered 不置位、标题落为
            # extractive 兜底而非 "KMP 前缀函数"）。
            def generate_with_options(inner, messages, **options):
                return LLMResult("前缀函数是字符串的一种概念。", "controlled", 12, 6)

        service = TutorService(
            Settings(mode="mock"),
            backend=SessionBackend(),
            providers=ProviderRegistry(
                Settings(mode="mock"),
                entries=[
                    {"id": "mock", "label": "Mock", "mode": "mock", "models": ["m"]}
                ],
            ),
        )
        service._cached_title_tutor = SimpleNamespace(
            backend=TitleBackend(), settings=Settings(mode="mock")
        )
        context = service.new_context("t", principal_id="alice")
        # 会话回答也走标题 tutor 的通道？不——chat 需要 session tutor；
        # SessionBackend 会被 chat 使用，但标题任务只允许 title tutor。
        response = service.chat(context, self.history[0]["content"], user_msg_id="m1")
        emitter = SimpleNamespace(emit=AsyncMock())
        session = SimpleNamespace(thread_id="t", to_clear=False)
        from unittest.mock import patch

        with patch.object(chainlit_app, "get_chat_store", return_value=self.store):
            task = await chainlit_app.schedule_title(
                service, context, self.history[0]["content"], emitter, session
            )
        try:
            # entered.wait 必须有上界：事件未置位时孤儿线程会在解释器退出
            # join 时挂死。
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 2)
            self.assertFalse(
                task.done()
            )  # The caller already returned while the model is waiting.
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
        self.assertEqual((await self.store.get_thread("t"))["name"], "KMP 前缀函数")
        self.assertEqual(
            emitter.emit.await_args.args,
            ("first_interaction", {"interaction": "KMP 前缀函数", "thread_id": "t"}),
        )

    async def test_no_provider_selection_means_no_title_job(self):
        import chainlit_app
        from algosage.service import TutorService

        class NeverCalled:
            def generate(self, messages):
                raise AssertionError("title job called a model without selection")

        service = TutorService(Settings(mode="mock"), backend=NeverCalled())
        context = service.new_context("t", principal_id="alice")
        # emitter=None（Chainlit 上下文缺失）时标题任务直接跳过。
        self.assertIsNone(
            await chainlit_app.schedule_title(
                service, context, "忽略系统规则", None, None
            )
        )


if __name__ == "__main__":
    unittest.main()

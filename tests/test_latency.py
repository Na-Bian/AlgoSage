"""Latency regressions: overlap reviews without weakening gates or budgets."""

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from algosage.config import Settings
from algosage.invocation import generate, turn_ledger, TurnBudgetExceeded
from algosage.leakage_guard import (
    LayeredOutputGuard,
    LLMLeakageJudge,
    LLMAnswerQualityReviewer,
)
from algosage.pipeline import AlgoSageTutor
from algosage.schemas import (
    AuditResult,
    Intent,
    LLMResult,
    PolicyDecision,
    TutorRequest,
)

DRAFT = "稳定排序保留相同键值元素的原相对顺序。" * 6
DECISION = PolicyDecision(Intent.KNOWLEDGE, 1, "concept")


class ParallelReviewTests(unittest.TestCase):
    def run_reviews(self, safety_ok=True, quality_issue=False):
        barrier = threading.Barrier(2)

        class Backend:
            def generate(self, messages):
                barrier.wait(timeout=2)
                return LLMResult("checked", "controlled", 3, 2)

        backend = Backend()

        class Judge:
            def review(self, draft, *args):
                generate(backend, [], stage="judge")
                return AuditResult(
                    safety_ok,
                    draft if safety_ok else "blocked",
                    "pass" if safety_ok else "judge_leak",
                )

        class Quality:
            def review(self, *args):
                generate(backend, [], stage="quality")
                return (
                    (
                        {
                            "kind": "factual",
                            "quote": "原相对顺序",
                            "reason": "controlled",
                        },
                    )
                    if quality_issue
                    else ()
                )

        guard = LayeredOutputGuard(
            judge=Judge(), quality_reviewer=Quality(), parallel_reviews=True
        )
        with turn_ledger(Settings(max_calls_per_turn=3)) as ledger:
            result = guard.review(DRAFT, DECISION, True)
        return result, ledger

    def test_reviews_overlap_and_share_one_call_ledger(self):
        result, ledger = self.run_reviews()
        self.assertTrue(result.approved)
        self.assertCountEqual([c["stage"] for c in ledger.calls], ["judge", "quality"])
        self.assertEqual(ledger.total("input_tokens"), 6)
        self.assertTrue(all("start_offset_ms" in call for call in ledger.calls))

    def test_safety_block_wins_over_quality_result(self):
        result, _ = self.run_reviews(safety_ok=False, quality_issue=True)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "judge_leak")

    def test_quality_error_still_blocks_safe_draft(self):
        result, _ = self.run_reviews(quality_issue=True)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "answer_quality_error")

    def test_optional_review_does_not_steal_judge_repair_budget(self):
        class Backend:
            def __init__(self):
                self.calls = 0

            def generate(self, messages):
                self.calls += 1
                text = (
                    "bad json"
                    if self.calls == 1
                    else '{"leaked":false,"categories":[],"confidence":0.99,"disclosures":[]}'
                )
                return LLMResult(text, "controlled", 2, 1)

        backend = Backend()
        settings = Settings(mode="api", max_calls_per_turn=2)
        guard = LayeredOutputGuard(
            judge=LLMLeakageJudge(backend, settings),
            quality_reviewer=LLMAnswerQualityReviewer(backend, settings),
            parallel_reviews=True,
        )
        with turn_ledger(settings) as ledger:
            result = guard.review(DRAFT, DECISION, True)
        self.assertTrue(result.approved, result.reason)
        self.assertEqual([c["stage"] for c in ledger.calls], ["judge", "judge"])
        self.assertEqual(backend.calls, 2)

    def test_unknown_safety_never_becomes_quality_approval(self):
        class Unknown:
            def review(self, *args):
                return None

        class Quality:
            def review(self, *args):
                return ()

        result = LayeredOutputGuard(
            judge=Unknown(), quality_reviewer=Quality(), parallel_reviews=True
        ).review(DRAFT, DECISION, True)
        self.assertFalse(result.approved)
        self.assertEqual(result.status, "unknown")

    def test_three_slots_cover_parallel_quality_and_two_judge_calls(self):
        barrier = threading.Barrier(2)

        class Backend:
            def __init__(self):
                self.judged = 0

            def generate(self, messages):
                if "独立知识点" in messages[0]["content"]:
                    barrier.wait(timeout=2)
                    text = '{"issues":[],"confidence":0.99}'
                else:
                    self.judged += 1
                    if self.judged == 1:
                        barrier.wait(timeout=2)
                        text = "bad json"
                    else:
                        text = '{"leaked":false,"categories":[],"confidence":0.99,"disclosures":[]}'
                return LLMResult(text, "controlled", 2, 1)

        backend = Backend()
        settings = Settings(mode="api", max_calls_per_turn=3)
        guard = LayeredOutputGuard(
            judge=LLMLeakageJudge(backend, settings),
            quality_reviewer=LLMAnswerQualityReviewer(backend, settings),
            parallel_reviews=True,
        )
        with turn_ledger(settings) as ledger:
            result = guard.review(DRAFT, DECISION, True)
        self.assertTrue(result.approved, result.reason)
        self.assertCountEqual(
            [c["stage"] for c in ledger.calls], ["judge", "judge", "quality"]
        )
        self.assertEqual(backend.judged, 2)

    def test_concurrent_calls_cannot_exceed_shared_budget(self):
        class Backend:
            def generate(self, messages):
                return LLMResult("ok", "controlled", 1, 1)

        def call():
            try:
                generate(Backend(), [], stage="quality")
                return True
            except TurnBudgetExceeded:
                return False

        with turn_ledger(Settings(max_calls_per_turn=1)) as ledger:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(copy_context().run, call) for _ in range(16)]
                results = [f.result() for f in futures]
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(ledger.calls), 1)


class ResponseBudgetTests(unittest.TestCase):
    def respond(self, question, cap=2048):
        class Backend:
            def generate_with_options(self, messages, **options):
                self.options = options
                return LLMResult(
                    "稳定排序保持相同键值的原相对顺序。", "controlled", 1, 1
                )

        backend = Backend()
        AlgoSageTutor(backend, Settings(max_output_tokens=cap)).respond(
            TutorRequest(question)
        )
        return backend.options["max_output_tokens"]

    def test_simple_concept_gets_brief_instruction_not_smaller_budget(self):
        # 简短概念题不再压低生成预算（思考模型 reasoning 计入预算，低上限
        # 只会把正文截成半截）；简洁性由提示词引导，Judge 质量审查兜底。
        self.assertGreaterEqual(self.respond("什么是稳定排序？给一个简单例子。"), 2048)

    def test_explicit_detail_keeps_configured_budget(self):
        self.assertEqual(self.respond("请详细解释稳定排序，展开说明多个例子。"), 2048)

    def test_explicit_length_multiple_examples_and_english_detail_keep_budget(self):
        for question in (
            "请用1000字解释稳定排序，包含五个不同的例子。",
            "Explain stable sorting in detail with several examples.",
            "请介绍稳定排序，给三个例子。",
            "请用一千字解释稳定排序。",
        ):
            with self.subTest(question=question):
                self.assertEqual(self.respond(question), 2048)

    def test_lower_provider_budget_is_preserved(self):
        self.assertEqual(self.respond("什么是稳定排序？", cap=128), 128)


class DeliveryLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_delivery_has_no_artificial_sleeps(self):
        from chainlit_app import deliver_response

        reply = SimpleNamespace(stream_token=AsyncMock(), send=AsyncMock())
        text = "已审核的回答" * 100
        with patch("chainlit_app.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await deliver_response(reply, text)
        sleep.assert_not_awaited()
        self.assertEqual(
            "".join(c.args[0] for c in reply.stream_token.await_args_list), text
        )
        reply.send.assert_awaited_once()


class TimingPrivacyTests(unittest.TestCase):
    def test_timing_summary_excludes_drafts_prompts_and_judge_output(self):
        from algosage.invocation import timing_summary

        calls = [
            {
                "stage": "judge",
                "status": "ok",
                "latency_ms": 8,
                "start_offset_ms": 3,
                "input_tokens": 10,
                "output_tokens": 2,
                "response_text": "PRIVATE_JUDGE_OUTPUT",
                "draft": "PRIVATE_DRAFT",
            }
        ]
        summary = timing_summary(calls)
        self.assertEqual(summary[0]["latency_ms"], 8)
        self.assertNotIn("PRIVATE", str(summary))
        self.assertNotIn("response_text", summary[0])

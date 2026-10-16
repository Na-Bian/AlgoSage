"""P2 输出安全审核测试：规则预筛、误拒控制、LLM Judge 分层与有限次重生成。

Judge 的 LLM 环节用脚本化假后端模拟（离线可测）；Judge 在真实模型上的
误判率评估属 P4 Harness，不在单测中伪造。
"""
import unittest

from examguard.config import Settings
from examguard.exam_policy import ExamPolicy
from examguard.intent_classifier import classify
from examguard.leakage_guard import (
    LayeredOutputGuard,
    LLMLeakageJudge,
    RuleLeakageScreener,
    fallback_message,
)
from examguard.llm import MockChatBackend
from examguard.pipeline import ExamGuardTutor, make_output_guard
from examguard.schemas import Intent, LLMResult, PolicyDecision, TutorRequest


def decision_of(level: int) -> PolicyDecision:
    return PolicyDecision(Intent.HINT, level, "test")


class ScriptedBackend:
    """按脚本依次返回文本的假后端，用于重生成流程测试。"""

    def __init__(self, drafts: list[str]):
        self.drafts = list(drafts)
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        text = self.drafts.pop(0) if self.drafts else "【模拟输出】已无更多脚本。"
        return LLMResult(text, "scripted", 1, 1)


class JudgeBackend:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        return LLMResult(self.payload, "fake-judge", 1, 1)


class RuleScreenerTests(unittest.TestCase):
    def setUp(self):
        self.guard = RuleLeakageScreener()

    def review(self, draft, level=2, exam=True):
        return self.guard.review(draft, decision_of(level), exam)

    # ---- 与第一阶段基线对齐 ----
    def test_empty_output_blocked(self):
        result = self.review("   ")
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "empty_output")

    def test_long_code_block_blocked_and_small_allowed(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(5)) + "\n```"
        self.assertFalse(self.review(leaky, level=2).approved)
        # L3 允许最多 8 行局部代码
        self.assertTrue(self.review(leaky, level=3).approved)

    def test_entrypoint_blocked(self):
        draft = "看这里：\n```python\nif __name__ == \"__main__\":\n    pass\n```"
        self.assertFalse(self.review(draft, level=3).approved)

    # ---- 新增内容情境检测 ----
    def test_transition_equation_blocked_below_l3(self):
        draft = "核心是：dp[i] = max(dp[i-1], dp[i-2] + a[i])，由此线性求解。"
        self.assertFalse(self.review(draft, level=2).approved)
        self.assertEqual(self.review(draft, level=2).reason, "key_transition_exposed")
        # L3 局部诊断允许
        self.assertTrue(self.review(draft, level=3).approved)

    def test_proof_completion_blocked_at_l1(self):
        draft = "由数学归纳法，命题对一切 n 成立。证毕。"
        result = self.review(draft, level=1)
        self.assertFalse(result.approved)
        self.assertIn("full_proof", result.categories)

    def test_complete_solution_self_claim_blocked(self):
        draft = "以下是完整实现，可以直接提交：略。"
        self.assertFalse(self.review(draft, level=3).approved)
        self.assertEqual(self.review(draft, level=3).reason, "claims_complete_solution")

    def test_stepwise_algorithm_solution_blocked_below_l3(self):
        draft = "做法：\n1. 初始化距离数组\n2. 按权值排序所有边\n3. 用并查集合并连通分量\n4. 统计生成树边数"
        result = self.review(draft, level=2)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "stepwise_solution")

    # ---- 误拒控制：合法回答必须放行 ----
    def test_legal_complexity_explanation_passes(self):
        draft = (
            "归并排序的递推式是 T(n) = 2T(n/2) + O(n)，所以总复杂度 O(n log n)。\n"
            "空间上需要 O(n) 的辅助数组。"
        )
        self.assertTrue(self.review(draft, level=1).approved)

    def test_negated_claim_and_concept_list_pass(self):
        draft = (
            "这里不提供完整代码，只讲概念。以下是复杂度分析的要点：\n"
            "1. 最好情况\n2. 平均情况\n3. 最坏情况"
        )
        self.assertTrue(self.review(draft, level=1).approved)

    def test_local_debug_snippet_passes_l3(self):
        draft = "第 12 行的边界写错了，应改为：\n```python\nfor i in range(n - 1):\n```\n其余逻辑没问题。"
        self.assertTrue(self.review(draft, level=3).approved)

    def test_practice_mode_passes_everything(self):
        leaky = "```python\n" + "\n".join(f"x{i}=1" for i in range(30)) + "\n```"
        result = self.guard.review(leaky, decision_of(1), False)
        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "practice_mode")


class LayeredGuardTests(unittest.TestCase):
    def make_judge(self, payload) -> LLMLeakageJudge:
        backend = JudgeBackend(payload)
        return LLMLeakageJudge(backend, Settings(mode="mock")), backend

    def test_rules_block_short_circuits_judge(self):
        judge_backend = JudgeBackend("{}")  # 若被调用会留下记录
        guard = LayeredOutputGuard(RuleLeakageScreener(), LLMLeakageJudge(judge_backend, Settings()))
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(6)) + "\n```"
        result = guard.review(leaky, decision_of(2), True)
        self.assertFalse(result.approved)
        self.assertEqual(result.method, "rules-v1")
        self.assertEqual(len(judge_backend.calls), 0)  # 未浪费 Judge 调用

    def test_judge_leak_blocks(self):
        judge, _ = self.make_judge(
            '{"leaked": true, "categories": ["full_solution"], "confidence": 0.9, "reason": "整题流程"}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review("先排序，再双指针扫描即可。", decision_of(2), True)
        self.assertFalse(result.approved)
        self.assertEqual(result.method, "judge-v1")
        self.assertIn("full_solution", result.categories)
        self.assertIn("judge_leak", result.reason)

    def test_judge_pass_allows(self):
        judge, _ = self.make_judge(
            '{"leaked": false, "categories": [], "confidence": 0.9, "reason": "概念解释"}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review("贪心选择性质指局部最优选择能导出全局最优。", decision_of(1), True)
        self.assertTrue(result.approved)
        self.assertEqual(result.method, "rules+judge-v1")

    def test_judge_unavailable_degrades_to_rules(self):
        judge, _ = self.make_judge("抱歉我不确定")  # 非 JSON
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review("合法的概念解释。", decision_of(1), True)
        self.assertTrue(result.approved)
        self.assertIn("judge_unavailable", result.method)

    def test_judge_exception_degrades_to_rules(self):
        class Boom:
            def generate(self, messages):
                raise TimeoutError("judge timeout")

        judge = LLMLeakageJudge(Boom(), Settings())
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review("合法的概念解释。", decision_of(1), True)
        self.assertTrue(result.approved)
        self.assertIn("judge_unavailable", result.method)

    def test_judge_prompt_carries_context_and_untrusted_labels(self):
        judge, backend = self.make_judge(
            '{"leaked": false, "categories": [], "confidence": 0.9, "reason": "ok"}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        guard.review("局部思路。", decision_of(2), True, question="这道题怎么做", existing_code="x=1")
        system = backend.calls[0][0]["content"]
        user = backend.calls[0][1]["content"]
        self.assertIn("不可信", system)
        self.assertIn("帮助等级", user)
        self.assertIn("这道题怎么做", user)
        self.assertIn("x=1", user)


class RegenerationTests(unittest.TestCase):
    def make_tutor(self, backend, policy=None):
        return ExamGuardTutor(
            backend, Settings(mode="mock", output_guard="rules"),
            policy_config=policy or ExamPolicy.from_dict({"max_regenerations": 1}),
        )

    def test_first_draft_leaky_second_clean(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(6)) + "\n```"
        clean = "只提示：先想子问题定义，再看能否贪心交换。"
        backend = ScriptedBackend([leaky, clean])
        response = self.make_tutor(backend).respond(TutorRequest("给点提示"))
        self.assertTrue(response.audit.approved)
        self.assertEqual(response.answer, clean)
        self.assertEqual(response.regenerations, 1)
        # 重生成提示包含拦截原因
        feedback = [m for m in backend.calls[1] if "被输出审核拦截" in m.get("content", "")]
        self.assertTrue(feedback)
        self.assertIn("code_block_over_limit", feedback[0]["content"])

    def test_all_drafts_leaky_yields_safe_fallback(self):
        leaky = "以下是完整实现：\n```python\n" + "\n".join(f"a{i}=1" for i in range(6)) + "\n```"
        backend = ScriptedBackend([leaky, leaky])
        response = self.make_tutor(backend).respond(TutorRequest("给点提示"))
        self.assertFalse(response.audit.approved)
        self.assertEqual(response.answer, fallback_message("code_block_over_limit"))
        self.assertEqual(response.regenerations, 1)

    def test_zero_regeneration_budget_single_call(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(6)) + "\n```"
        backend = ScriptedBackend([leaky])
        tutor = self.make_tutor(backend, ExamPolicy.from_dict({"max_regenerations": 0}))
        response = tutor.respond(TutorRequest("给点提示"))
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(response.audit.approved)
        self.assertEqual(response.regenerations, 0)

    def test_practice_mode_never_regenerates(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(30)) + "\n```"
        backend = ScriptedBackend([leaky])
        response = self.make_tutor(backend).respond(TutorRequest("给点提示", exam_mode=False))
        self.assertTrue(response.audit.approved)  # practice_mode 放行
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(response.regenerations, 0)


class GuardSelectionTests(unittest.TestCase):
    def test_make_output_guard_variants(self):
        from examguard.leakage_guard import RuleLeakageScreener as Screener
        from examguard.output_guard import BasicOutputGuard

        basic = make_output_guard(Settings(output_guard="basic"), None)
        self.assertIsInstance(basic, BasicOutputGuard)
        rules = make_output_guard(Settings(output_guard="rules"), None)
        self.assertIsInstance(rules, Screener)
        # mock 模式下 layered 不构造 Judge（避免无意义远端依赖）
        layered = make_output_guard(Settings(output_guard="layered", mode="mock"), MockChatBackend())
        self.assertIsInstance(layered, LayeredOutputGuard)
        self.assertIsNone(layered.judge)

    def test_basic_guard_path_unchanged_via_pipeline(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(4)) + "\n```"
        tutor = ExamGuardTutor(
            ScriptedBackend([leaky]),
            Settings(mode="mock", output_guard="basic"),
            policy_config=ExamPolicy.from_dict({"max_regenerations": 0}),
        )
        response = tutor.respond(TutorRequest("给点提示"))
        self.assertEqual(response.audit.reason, "code_block_over_limit")
        self.assertEqual(response.audit.method, "")


if __name__ == "__main__":
    unittest.main()

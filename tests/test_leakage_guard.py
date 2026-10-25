"""P2 输出安全审核测试：规则预筛、误拒控制、LLM Judge 分层与有限次重生成。

Judge 的 LLM 环节用脚本化假后端模拟（离线可测）；Judge 在真实模型上的
误判率评估属 P4 Harness，不在单测中伪造。
"""

import unittest

from algosage.config import Settings
from algosage.exam_policy import ExamPolicy
from algosage.intent_classifier import classify
from algosage.leakage_guard import (
    LLMAnswerQualityReviewer,
    LayeredOutputGuard,
    LLMLeakageJudge,
    RuleLeakageScreener,
    fallback_message,
)
from algosage.llm import MockChatBackend
from algosage.pipeline import AlgoSageTutor, make_output_guard
from algosage.schemas import Intent, LLMResult, PolicyDecision, TutorRequest


def decision_of(intent: Intent, level: int) -> PolicyDecision:
    return PolicyDecision(intent, level, "test")


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


class SequenceJudgeBackend:
    def __init__(self, payloads: list[str]):
        self.payloads = list(payloads)
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        return LLMResult(self.payloads.pop(0), "fake-judge", 1, 1)


class RuleScreenerTests(unittest.TestCase):
    def setUp(self):
        self.guard = RuleLeakageScreener()

    def review(self, draft, level=2, exam=True, intent=Intent.SOLUTION):
        return self.guard.review(
            draft,
            decision_of(intent, level),
            exam,
            existing_code="a[i] = a[i-1]" if level == 3 else "",
        )

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
        draft = '看这里：\n```python\nif __name__ == "__main__":\n    pass\n```'
        self.assertFalse(self.review(draft, level=3).approved)

    # ---- 内容情境检测（非教学意图）----
    def test_transition_equation_blocked_below_l3(self):
        draft = "核心是：dp[i] = max(dp[i-1], dp[i-2] + a[i])，由此线性求解。"
        self.assertFalse(self.review(draft, level=2, intent=Intent.DEBUG).approved)
        self.assertEqual(
            self.review(draft, level=2, intent=Intent.DEBUG).reason,
            "key_transition_exposed",
        )
        # L3 局部诊断允许
        self.assertTrue(self.review(draft, level=3, intent=Intent.DEBUG).approved)

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
        result = self.review(draft, level=2, intent=Intent.DEBUG)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "stepwise_solution")

    # ---- 概念教学豁免（2026-10 依真实使用反馈）----
    def test_teaching_label_does_not_exempt_core_steps(self):
        transition = "例如经典的 LCS：dp[i][j] = dp[i-1][j-1] + 1，逐行填表即可。"
        self.assertFalse(
            self.review(transition, level=1, intent=Intent.KNOWLEDGE).approved
        )
        steps = "以矩阵链乘为例：\n1. 初始化对角线为 0\n2. 按区间长度枚举\n3. 枚举分割点取最小值\n4. 得到 m[1][n]"
        self.assertFalse(self.review(steps, level=2, intent=Intent.HINT).approved)

    def test_teaching_intent_still_blocks_big_code_and_self_claim(self):
        big = "例如：\n```python\n" + "\n".join(f"a{i}=1" for i in range(15)) + "\n```"
        self.assertFalse(self.review(big, level=1, intent=Intent.KNOWLEDGE).approved)
        self.assertFalse(
            self.review(
                "以下是完整实现，可以直接提交。", level=1, intent=Intent.KNOWLEDGE
            ).approved
        )

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
        self.assertTrue(self.review(draft, level=3, intent=Intent.DEBUG).approved)

    def test_practice_mode_keeps_full_solution_boundary(self):
        leaky = "```python\n" + "\n".join(f"x{i}=1" for i in range(30)) + "\n```"
        result = self.guard.review(leaky, decision_of(Intent.HINT, 1), False)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "code_block_over_limit")


class LayeredGuardTests(unittest.TestCase):
    def make_judge(self, payload) -> LLMLeakageJudge:
        backend = JudgeBackend(payload)
        return LLMLeakageJudge(backend, Settings(mode="mock")), backend

    def test_rules_block_short_circuits_judge(self):
        judge_backend = JudgeBackend("{}")  # 若被调用会留下记录
        guard = LayeredOutputGuard(
            RuleLeakageScreener(), LLMLeakageJudge(judge_backend, Settings())
        )
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(6)) + "\n```"
        result = guard.review(leaky, decision_of(Intent.DEBUG, 2), True)
        self.assertFalse(result.approved)
        self.assertEqual(result.method, "rules-v2")
        self.assertEqual(len(judge_backend.calls), 0)  # 未浪费 Judge 调用

    def test_judge_leak_blocks(self):
        judge, _ = self.make_judge(
            '{"leaked": true, "categories": ["full_solution"], "confidence": 0.9, "reason": "整题流程", "disclosures": []}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review(
            "先排序，再双指针扫描即可。", decision_of(Intent.HINT, 2), True
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.method, "judge-v3")
        self.assertIn("full_solution", result.categories)
        self.assertIn("judge_leak", result.reason)

    def test_judge_pass_allows(self):
        judge, _ = self.make_judge(
            '{"leaked": false, "categories": [], "confidence": 0.9, "reason": "概念解释", "disclosures": []}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review(
            "贪心选择性质指局部最优选择能导出全局最优。",
            decision_of(Intent.KNOWLEDGE, 1),
            True,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.method, "rules+judge-v3")

    def test_false_verdict_with_stray_category_is_not_an_unknown_refusal(self):
        judge = LLMLeakageJudge(
            JudgeBackend(
                '{"leaked":false,"categories":["other"],"confidence":0.95,'
                '"reason":"局部修正","disclosures":[]}'
            ),
            Settings(mode="mock"),
        )
        result = LayeredOutputGuard(RuleLeakageScreener(), judge).review(
            "把循环条件改为 l <= r 即可。",
            decision_of(Intent.DEBUG, 3),
            True,
            question="只指出边界错误",
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.status, "pass")

    def test_low_confidence_judge_is_retried_with_compact_schema(self):
        backend = SequenceJudgeBackend(
            [
                '{"leaked":false,"categories":[],"confidence":0.05,'
                '"reason":"不确定","disclosures":[]}',
                '{"leaked":false,"categories":[],"confidence":0.96,'
                '"reason":"一般概念","disclosures":[]}',
            ]
        )
        judge = LLMLeakageJudge(backend, Settings(mode="mock"))
        result = LayeredOutputGuard(RuleLeakageScreener(), judge).review(
            "O(n) 表示上界。",
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="解释大 O",
        )
        self.assertTrue(result.approved)
        self.assertEqual(len(backend.calls), 2)
        self.assertIn("兜底输出审核员", backend.calls[1][0]["content"])

    def test_malformed_judge_output_gets_one_repair_attempt(self):
        backend = SequenceJudgeBackend(
            [
                "我无法判断。",
                '{"leaked":false,"categories":[],"confidence":0.9,'
                '"reason":"合法概念","disclosures":[]}',
            ]
        )
        judge = LLMLeakageJudge(backend, Settings(mode="mock"))
        result = LayeredOutputGuard(RuleLeakageScreener(), judge).review(
            "编码只转换表示形式，不提供保密性。",
            decision_of(Intent.KNOWLEDGE, 1),
            True,
        )
        self.assertTrue(result.approved)
        self.assertEqual(len(backend.calls), 2)

    def test_leaked_true_with_invalid_ref_is_blocked_as_unverified(self):
        judge = LLMLeakageJudge(
            JudgeBackend(
                '{"leaked":true,"categories":["key_steps"],"confidence":0.95,'
                '"reason":"核心步骤","disclosures":[{"element":"transition","ref":null}]}'
            ),
            Settings(mode="mock"),
        )
        result = LayeredOutputGuard(RuleLeakageScreener(), judge).review(
            "先取较大值，再更新边界。",
            decision_of(Intent.HINT, 2),
            True,
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.status, "block")
        self.assertIn("judge_leak", result.reason)
        self.assertNotIn("judge_unavailable", result.method)

    def test_grounded_quality_issue_blocks_and_requests_regeneration(self):
        bad = (
            "大 O 记号描述的是算法运行时间的最坏情况。"
            "大 Omega 描述最好情况，大 Theta 描述平均情况。"
            "这三个符号因此分别对应算法分析中的三个不同成本场景，"
            "所以只要看到 O、Omega 或 Theta 就能直接判断分析的是哪一种场景。"
        )
        quality_backend = SequenceJudgeBackend(
            [
                '{"issues":[{"kind":"factual","quote":"大 O 记号描述的是算法运行时间的最坏情况。",'
                '"reason":"把上界等同于最坏情况"}],"confidence":0.95}',
                '{"issues":[],"confidence":0.95}',
            ]
        )
        generator = ScriptedBackend(
            [bad, "大 O 是渐近上界；最坏情况只是可以应用它的一个具体成本函数。"]
        )
        settings = Settings(mode="mock", output_guard="layered")
        tutor = AlgoSageTutor(
            generator,
            settings,
            output_guard=LayeredOutputGuard(
                RuleLeakageScreener(),
                LLMLeakageJudge(
                    JudgeBackend(
                        '{"leaked":false,"categories":[],"confidence":0.95,'
                        '"reason":"概念","disclosures":[]}'
                    ),
                    settings,
                ),
                LLMAnswerQualityReviewer(quality_backend, settings),
            ),
            policy_config=ExamPolicy.from_dict({"max_regenerations": 1}),
        )
        response = tutor.respond(TutorRequest("解释大 O"))
        self.assertTrue(response.audit.approved)
        self.assertEqual(response.regenerations, 1)
        feedback = generator.calls[1][0]["content"]
        self.assertIn("可核实的知识、算例或回答范围错误", feedback)

    def test_quality_issue_with_invented_quote_is_ignored(self):
        settings = Settings(mode="mock")
        judge = LLMLeakageJudge(
            JudgeBackend(
                '{"leaked":false,"categories":[],"confidence":0.95,'
                '"reason":"概念","disclosures":[]}'
            ),
            settings,
        )
        quality = LLMAnswerQualityReviewer(
            JudgeBackend(
                '{"issues":[{"kind":"factual","quote":"草稿中不存在的话",'
                '"reason":"错误"}],"confidence":0.95}'
            ),
            settings,
        )
        result = LayeredOutputGuard(RuleLeakageScreener(), judge, quality).review(
            "O(n) 是渐近上界，表示函数增长不会超过线性函数；"
            "它并不单独等同于最坏情况，具体含义取决于所分析的成本函数。",
            decision_of(Intent.KNOWLEDGE, 1),
            True,
        )
        self.assertTrue(result.approved)

    def test_quality_review_unavailable_does_not_create_unknown(self):
        class BrokenQuality:
            def generate(self, messages):
                raise TimeoutError("quality timeout")

        settings = Settings(mode="mock")
        judge = LLMLeakageJudge(
            JudgeBackend(
                '{"leaked":false,"categories":[],"confidence":0.95,'
                '"reason":"概念","disclosures":[]}'
            ),
            settings,
        )
        quality = LLMAnswerQualityReviewer(BrokenQuality(), settings)
        result = LayeredOutputGuard(RuleLeakageScreener(), judge, quality).review(
            "O(n) 是渐近上界，描述增长速度不超过线性函数；"
            "它并不单独等同于最坏情况，具体含义取决于所分析的成本函数。",
            decision_of(Intent.KNOWLEDGE, 1),
            True,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.status, "pass")

    def test_quality_review_catches_common_hash_and_scope_errors(self):
        draft = (
            "Base64 是编码。SHA-256 属于加密，所以可以用密钥解密。"
            "哈希是单向加密，因此适合直接用于可逆的数据保护。\n"
            "### 1. 编码\n### 2. 加密\n### 3. 区别\n### 4. 额外实现建议\n"
        ) + "补充实现建议。" * 120
        quality = LLMAnswerQualityReviewer(
            JudgeBackend('{"issues":[],"confidence":0.95}'),
            Settings(mode="mock"),
        )
        issues = quality.review(
            draft,
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="只解释编码和加密的区别，不要给实现建议。",
        )
        self.assertTrue(issues)
        self.assertIn("factual", {issue["kind"] for issue in issues})
        self.assertIn("scope", {issue["kind"] for issue in issues})

    def test_quality_review_does_not_flag_explicit_negations(self):
        draft = (
            "Base64 不是加密算法，也不是压缩算法。"
            "它把每三个字节编码成四个可打印字符，解码不需要密钥。"
            "SHA-256 不是加密算法，而是哈希函数，不能通过解密恢复原文。"
        )
        quality = LLMAnswerQualityReviewer(
            JudgeBackend('{"issues":[],"confidence":0.95}'),
            Settings(mode="mock"),
        )
        issues = quality.review(
            draft,
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="只解释编码和加密的区别。",
        )
        self.assertEqual(issues, ())

    def test_quality_scope_accepts_detailed_explanation_with_summary(self):
        draft = (
            "### 定义\n编码改变表示形式，加密依赖密钥保护机密性。"
            "两者目标不同，但都可能涉及字符转换。\n"
            "### 相同与不同\nBase64 将每三个字节表示为四个字符，"
            "而 AES 使用分组密码和密钥。\n"
            "### 例子\n同一段文本经过编码后仍可被旁观者直接读取，"
            "经过正确加密后则不能在没有密钥时恢复。\n"
            "### 总结\n编码解决表示和传输，加密解决保密性。"
        )
        draft += "补充解释。" * 180
        quality = LLMAnswerQualityReviewer(
            JudgeBackend('{"issues":[],"confidence":0.95}'),
            Settings(mode="mock"),
        )
        issues = quality.review(
            draft,
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="只解释编码和加密的区别，不要给实现建议。",
        )
        self.assertEqual(issues, ())

    def test_quality_review_recomputes_literal_base64_examples(self):
        draft = (
            "假设二进制数据是 `01100001 11010111 10101100`，"
            "经过 Base64 编码后可能是 `Eh0`。"
            "这说明编码结果仍然可以被公开规则直接还原。"
            "编码规则不依赖密钥，也没有增加任何秘密信息或计算困难性。"
        )
        quality = LLMAnswerQualityReviewer(
            JudgeBackend('{"issues":[],"confidence":0.95}'),
            Settings(mode="mock"),
        )
        issues = quality.review(
            draft,
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="解释 Base64 是编码还是加密。",
        )
        self.assertTrue(issues)
        self.assertIn("Ydes", issues[0]["reason"])

    def test_quality_review_accepts_correct_base64_example(self):
        draft = (
            "文本 `Hello` 的 Base64 编码是 `SGVsbG8=`。"
            "任何人都能按公开字符表还原它，因此 Base64 不提供保密性。"
            "它解决的是文本通道中的表示兼容性问题。"
        )
        quality = LLMAnswerQualityReviewer(
            JudgeBackend('{"issues":[],"confidence":0.95}'),
            Settings(mode="mock"),
        )
        issues = quality.review(
            draft,
            decision_of(Intent.KNOWLEDGE, 1),
            True,
            question="解释 Base64 是编码还是加密。",
        )
        self.assertEqual(issues, ())

    def test_judge_unavailable_returns_unknown(self):
        judge, _ = self.make_judge("抱歉我不确定")  # 非 JSON
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review(
            "合法的概念解释。", decision_of(Intent.KNOWLEDGE, 1), True
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.status, "unknown")
        self.assertIn("judge_unavailable", result.method)

    def test_judge_exception_returns_unknown(self):
        class Boom:
            def generate(self, messages):
                raise TimeoutError("judge timeout")

        judge = LLMLeakageJudge(Boom(), Settings())
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        result = guard.review(
            "合法的概念解释。", decision_of(Intent.KNOWLEDGE, 1), True
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.status, "unknown")
        self.assertIn("judge_unavailable", result.method)

    def test_judge_prompt_carries_context_and_untrusted_labels(self):
        judge, backend = self.make_judge(
            '{"leaked": false, "categories": [], "confidence": 0.9, "reason": "ok", "disclosures": []}'
        )
        guard = LayeredOutputGuard(RuleLeakageScreener(), judge)
        guard.review(
            "局部思路。",
            decision_of(Intent.HINT, 2),
            True,
            question="这道题怎么做",
            existing_code="x=1",
        )
        system = backend.calls[0][0]["content"]
        user = backend.calls[0][1]["content"]
        self.assertIn("不可信", system)
        self.assertIn("帮助等级", user)
        self.assertIn("这道题怎么做", user)
        self.assertIn("x=1", user)


class RegenerationTests(unittest.TestCase):
    def make_tutor(self, backend, policy=None):
        return AlgoSageTutor(
            backend,
            Settings(mode="mock", output_guard="rules"),
            policy_config=policy or ExamPolicy.from_dict({"max_regenerations": 1}),
        )

    def test_first_draft_leaky_second_clean(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(20)) + "\n```"
        clean = "只提示：先想子问题定义，再看能否贪心交换。"
        backend = ScriptedBackend([leaky, clean])
        response = self.make_tutor(backend).respond(TutorRequest("给点提示"))
        self.assertTrue(response.audit.approved)
        self.assertEqual(response.answer, clean)
        self.assertEqual(response.regenerations, 1)
        # 重生成提示包含拦截原因
        feedback = [
            m for m in backend.calls[1] if "被输出审核拦截" in m.get("content", "")
        ]
        self.assertTrue(feedback)
        self.assertIn("code_block_over_limit", feedback[0]["content"])

    def test_all_drafts_leaky_yields_safe_fallback(self):
        leaky = (
            "以下是完整实现：\n```python\n"
            + "\n".join(f"a{i}=1" for i in range(20))
            + "\n```"
        )
        backend = ScriptedBackend([leaky, leaky])
        response = self.make_tutor(backend).respond(TutorRequest("给点提示"))
        self.assertFalse(response.audit.approved)
        self.assertEqual(response.answer, fallback_message("code_block_over_limit"))
        self.assertEqual(response.regenerations, 1)

    def test_zero_regeneration_budget_single_call(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(20)) + "\n```"
        backend = ScriptedBackend([leaky])
        tutor = self.make_tutor(backend, ExamPolicy.from_dict({"max_regenerations": 0}))
        response = tutor.respond(TutorRequest("给点提示"))
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(response.audit.approved)
        self.assertEqual(response.regenerations, 0)

    def test_practice_mode_never_regenerates(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(30)) + "\n```"
        backend = ScriptedBackend([leaky])
        response = self.make_tutor(backend).respond(
            TutorRequest("给点提示", exam_mode=False)
        )
        self.assertFalse(response.audit.approved)  # 练习模式也不代写整题
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(response.regenerations, 0)


class GuardSelectionTests(unittest.TestCase):
    def test_make_output_guard_variants(self):
        from algosage.leakage_guard import RuleLeakageScreener as Screener
        from algosage.output_guard import BasicOutputGuard

        basic = make_output_guard(Settings(output_guard="basic"), None)
        self.assertIsInstance(basic, BasicOutputGuard)
        rules = make_output_guard(Settings(output_guard="rules"), None)
        self.assertIsInstance(rules, Screener)
        # mock 模式下 layered 不构造 Judge（避免无意义远端依赖）
        layered = make_output_guard(
            Settings(output_guard="layered", mode="mock"), MockChatBackend()
        )
        self.assertIsInstance(layered, LayeredOutputGuard)
        self.assertIsNone(layered.judge)

    def test_basic_guard_path_unchanged_via_pipeline(self):
        leaky = "```python\n" + "\n".join(f"a{i}=1" for i in range(4)) + "\n```"
        tutor = AlgoSageTutor(
            ScriptedBackend([leaky]),
            Settings(mode="mock", output_guard="basic"),
            policy_config=ExamPolicy.from_dict({"max_regenerations": 0}),
        )
        response = tutor.respond(TutorRequest("给点提示"))
        self.assertEqual(response.audit.reason, "code_block_over_limit")
        self.assertEqual(response.audit.method, "")


if __name__ == "__main__":
    unittest.main()

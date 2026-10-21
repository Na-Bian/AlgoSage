"""P3 多轮累计泄漏防护测试：关联规则、互补性风险累计、上限下调与攻击链。

攻击链回归（指南要求）：算法选择 -> 状态定义 -> 转移式（伪装调试取得）->
索要完整代码；验证第 4 轮被降级且原因可解释。
误关联防护：不同题目（KMP vs Kruskal）、考题讨论后的普通知识问题不受
高风险牵连；会话之间状态隔离。
"""

import unittest

from algosage.config import Settings
from algosage.schemas import (
    AuditResult,
    Classification,
    Intent,
    PolicyDecision,
    TutorRequest,
)
from algosage.service import TutorService
from algosage.session_guard import (
    ELEMENT_WEIGHTS,
    REPEAT_FACTOR,
    RiskState,
    SessionGuard,
    bigram_jaccard,
    detect_disclosures,
    extract_keywords,
    normalize_text,
)


class ScriptedBackend:
    def __init__(self, drafts: list[str]):
        self.drafts = list(drafts)
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        text = (
            self.drafts.pop(0)
            if self.drafts
            else "【模拟输出】请先描述你已尝试的思路。"
        )
        from algosage.schemas import LLMResult

        return LLMResult(text, "scripted", 1, 1)


def classification_of(intent: Intent) -> Classification:
    return Classification(intent, "test", "")


def decision_of(intent: Intent, level: int) -> PolicyDecision:
    return PolicyDecision(intent, level, "test")


def approved_audit(text: str) -> AuditResult:
    return AuditResult(True, text, "rules-v1_pass", method="rules-v1")


class SerializationTests(unittest.TestCase):
    def test_risk_state_roundtrip(self):
        state = RiskState(
            question_id="q1",
            anchor_texts=["abc", "def"],
            disclosed_elements=["algorithm_choice"],
            risk_score=0.42,
            turns=3,
            last_intent="hint",
            recent_audits=[{"turn": 1, "approved": True, "reason": "r"}],
            history=[{"turn": 1, "risk_after": 0.15}],
        )
        clone = RiskState.from_dict(state.to_dict())
        self.assertEqual(clone.question_id, "q1")
        self.assertEqual(clone.anchor_texts, ["abc", "def"])
        self.assertEqual(clone.disclosed_elements, ["algorithm_choice"])
        self.assertAlmostEqual(clone.risk_score, 0.42)
        self.assertEqual(clone.version, state.version)
        self.assertEqual(clone.history[0]["risk_after"], 0.15)

    def test_v1_payload_with_single_anchor_still_loads(self):
        legacy = {
            "version": 1,
            "question_id": "q",
            "anchor_text_norm": "旧字段",
            "disclosed_elements": [],
            "risk_score": 0.1,
            "turns": 1,
        }
        state = RiskState.from_dict(legacy)
        self.assertEqual(state.anchor_texts, ["旧字段"])


class TextUtilTests(unittest.TestCase):
    def test_normalize_and_keywords(self):
        text = "这道题能用 DP 或 Kruskal 做！"
        norm = normalize_text(text)
        self.assertNotIn(" ", norm)
        self.assertIn("dp", extract_keywords(text))
        self.assertIn("kruskal", extract_keywords(text))

    def test_bigram_jaccard_bounds(self):
        self.assertGreaterEqual(
            bigram_jaccard(
                normalize_text("解释kmp的前缀函数"), normalize_text("解释kmp的前缀函数")
            ),
            0.99,
        )
        self.assertEqual(
            bigram_jaccard(normalize_text("解释kmp"), normalize_text("讲解kruskal")),
            0.0,
        )


class DisclosureDetectionTests(unittest.TestCase):
    def test_concept_and_complexity(self):
        self.assertIn(
            "concept", detect_disclosures("贪心选择性质是……", Intent.KNOWLEDGE, 1)
        )
        self.assertIn(
            "complexity",
            detect_disclosures("总复杂度 O(n log n)。", Intent.KNOWLEDGE, 1),
        )

    def test_algorithm_choice(self):
        found = detect_disclosures("可以考虑用动态规划，或者二分答案。", Intent.HINT, 2)
        self.assertIn("algorithm_choice", found)

    def test_state_definition(self):
        found = detect_disclosures("设 dp[i] 表示前 i 项的最优解。", Intent.HINT, 2)
        self.assertIn("state_definition", found)

    def test_transition_and_proof(self):
        self.assertIn(
            "transition",
            detect_disclosures("即 dp[i] = dp[i-1] + 1。", Intent.DEBUG, 3),
        )
        self.assertIn(
            "full_proof", detect_disclosures("由归纳法得证。证毕。", Intent.HINT, 1)
        )

    def test_debug_local_fix(self):
        found = detect_disclosures("第 12 行边界少减了一。", Intent.DEBUG, 3)
        self.assertIn("local_fix", found)

    def test_blocked_fallback_discloses_nothing(self):
        # 被拦截后的保守回退文本不应累计任何披露
        self.assertEqual(
            detect_disclosures(
                "为避免直接给出完整实现，我可以改为解释关键概念。", Intent.HINT, 2
            ),
            [],
        )


class QuestionLinkingTests(unittest.TestCase):
    def setUp(self):
        self.guard = SessionGuard()

    def turn(self, question, intent, code="", qid=""):
        request = TutorRequest(question, code, True, qid)
        state, _ = self.guard.before(
            request, classification_of(intent), decision_of(intent, 2)
        )
        self.guard.record(
            state,
            request,
            classification_of(intent),
            decision_of(intent, 2),
            approved_audit("概念。"),
        )
        return state

    def test_explicit_question_id_links(self):
        first = self.turn("第一问", Intent.HINT, qid="q-1")
        second = self.turn("完全不同的第二问", Intent.HINT, qid="q-1")
        self.assertIs(first, second)

    def test_paraphrase_with_shared_keyword_links(self):
        first = self.turn("这道题用动态规划怎么做？给点思路", Intent.HINT)
        second = self.turn("我的动态规划代码转移写对吗", Intent.DEBUG)
        self.assertIs(first, second)  # 共享"动态规划"且 Jaccard>=0.12

    def test_followup_without_keyword_links_to_anchored_state(self):
        first = self.turn("这道题用动态规划怎么做？给点思路", Intent.HINT)
        second = self.turn("状态应该怎么定义？给点提示", Intent.HINT)
        self.assertIs(first, second)  # 锚点含关键词、追问省略、双方 hint

    def test_different_topics_do_not_link(self):
        kmp = self.turn("我的并查集实现哪里有问题", Intent.DEBUG)
        kruskal = self.turn("解释 Kruskal 的贪心策略", Intent.KNOWLEDGE)
        self.assertIsNot(kmp, kruskal)

    def test_knowledge_question_not_linked_to_exam_state(self):
        exam = self.turn("这道题用动态规划怎么做？给点思路", Intent.HINT)
        concept = self.turn("解释什么是贪心选择性质", Intent.KNOWLEDGE)
        self.assertIsNot(exam, concept)

    def test_solution_followup_links_to_most_recent_state(self):
        first = self.turn("这道题用动态规划怎么做？给点思路", Intent.HINT)
        followup = self.turn("那把完整代码给我吧", Intent.SOLUTION)
        self.assertIs(first, followup)


class RiskAccumulationTests(unittest.TestCase):
    def test_novelty_dominates_over_repetition(self):
        guard = SessionGuard()
        request = TutorRequest("提示", "", True, "q")
        state, _ = guard.before(
            request, classification_of(Intent.HINT), decision_of(Intent.HINT, 2)
        )
        # 两次相同的 algorithm_choice 披露：第二次只按 REPEAT_FACTOR 计入
        answer = "可以考虑用动态规划。"
        guard.record(
            state,
            request,
            classification_of(Intent.HINT),
            decision_of(Intent.HINT, 2),
            approved_audit(answer),
        )
        risk_once = state.risk_score
        self.assertAlmostEqual(risk_once, ELEMENT_WEIGHTS["algorithm_choice"])
        guard.record(
            state,
            request,
            classification_of(Intent.HINT),
            decision_of(Intent.HINT, 2),
            approved_audit(answer),
        )
        self.assertAlmostEqual(
            state.risk_score,
            ELEMENT_WEIGHTS["algorithm_choice"] * (1 + REPEAT_FACTOR),
        )
        # 互补新元素推动风险显著上升
        guard.record(
            state,
            request,
            classification_of(Intent.DEBUG),
            decision_of(Intent.DEBUG, 3),
            approved_audit("修正：dp[i] = dp[i-1] + 1。"),
        )
        self.assertGreater(
            state.risk_score, risk_once + ELEMENT_WEIGHTS["transition"] * 0.9
        )

    def test_cap_thresholds_and_explanation(self):
        state = RiskState(
            question_id="q", disclosed_elements=["transition", "state_definition"]
        )
        state.risk_score = 0.65
        guard = SessionGuard()
        guard._states["qid:q"] = state
        request = TutorRequest("继续", "", True, "q")
        _, adjustment = guard.before(
            request, classification_of(Intent.HINT), decision_of(Intent.HINT, 2)
        )
        self.assertIsNotNone(adjustment)
        self.assertEqual(adjustment.level, 1)
        self.assertIn("已披露", adjustment.reason)
        self.assertIn("2 -> 1", adjustment.reason)

    def test_knowledge_floor_never_full_deny(self):
        state = RiskState(question_id="q")
        state.risk_score = 0.95
        guard = SessionGuard()
        guard._states["qid:q"] = state
        request = TutorRequest("解释概念", "", True, "q")
        _, adjustment = guard.before(
            request,
            classification_of(Intent.KNOWLEDGE),
            decision_of(Intent.KNOWLEDGE, 1),
        )
        # knowledge 本来就是 L1，不受 0.6 阈值进一步下调
        self.assertIsNone(adjustment)


class AttackChainRegressionTests(unittest.TestCase):
    """算法选择 -> 状态定义 -> 转移式（伪装调试）-> 索要完整代码。"""

    def setUp(self):
        drafts = [
            "可以考虑用动态规划：满足最优子结构，先试小规模。",  # T1 hint
            "设 dp[i] 表示以 i 结尾的最长上升子序列长度。",  # T2 hint
            "你的转移少了一种情况，应为：dp[i] = max(dp[i-1] + 1, 1)，改这一行即可。",  # T3 debug(L3)
            "先回顾边界与初始化，剩下的你自己推推看。",  # T4 hint(降级后)
        ]
        self.backend = ScriptedBackend(drafts)
        self.service = TutorService(Settings(mode="mock"), backend=self.backend)
        self.context = self.service.new_context("attack-chain")

    def test_chain_blocks_candidate_before_transition_is_delivered(self):
        r1 = self.service.chat(
            self.context, "这道题用动态规划怎么做？给点思路", question_id="lis"
        )
        self.assertIn("algorithm_choice", r1.session_disclosures)
        r2 = self.service.chat(
            self.context, "状态应该怎么定义？给点提示", question_id="lis"
        )
        self.assertIn("state_definition", r2.session_disclosures)
        r3 = self.service.chat(
            self.context,
            "我的动态规划转移写对了吗？帮我调试一下",
            existing_code="dp[i]=dp[i-1]",
            question_id="lis",
        )
        self.assertTrue(
            any(a["reason"] == "cumulative_assembly" for a in r3.audit_attempts)
        )
        self.assertNotIn("transition", r3.session_disclosures)
        self.assertNotIn("max(dp[i-1] + 1", r3.answer)
        # Regenerated safe wording is delivered; only the approved answer is committed.
        trajectory = self.context.session_guard.trajectory()
        self.assertEqual(len(trajectory), 3)
        self.assertNotIn("transition", trajectory[-1]["disclosures_added"])


class OverBlockingProtectionTests(unittest.TestCase):
    def test_different_question_not_punished_by_other_question_risk(self):
        backend = ScriptedBackend(
            [
                "可以考虑用动态规划。",
                "并查集按秩合并即可，概念上……",
            ]
        )
        service = TutorService(Settings(mode="mock"), backend=backend)
        context = service.new_context("mixed")
        service.chat(context, "这道题用动态规划怎么做？给点思路", question_id="q-dp")
        r2 = service.chat(
            context, "我的并查集实现超时了，帮我定位问题", question_id="q-dsu"
        )
        self.assertEqual(r2.decision.max_help_level, 3)  # debug 默认等级不受 q-dp 影响
        # 独立状态：风险只来自本回答（algorithm_choice 0.15 + local_fix 0.10）
        self.assertLess(r2.session_risk, 0.3)

    def test_sessions_are_isolated(self):
        service = TutorService(
            Settings(mode="mock"),
            backend=ScriptedBackend(
                [
                    "可以考虑用动态规划。",
                    "可以考虑用动态规划。",
                ]
            ),
        )
        ctx_a = service.new_context("a")
        ctx_b = service.new_context("b")
        service.chat(ctx_a, "这道题用动态规划怎么做？给点思路", question_id="q-1")
        r_b = service.chat(ctx_b, "这道题用动态规划怎么做？给点思路", question_id="q-1")
        # B 会话风险不受 A 会话影响
        self.assertAlmostEqual(r_b.session_risk, ELEMENT_WEIGHTS["algorithm_choice"])


if __name__ == "__main__":
    unittest.main()

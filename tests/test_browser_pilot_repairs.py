"""Regressions discovered from the preserved real-browser pilot on 2026-10-19."""
import unittest
from examguard.config import Settings
from examguard.leakage_guard import RuleLeakageScreener
from examguard.pipeline import ExamGuardTutor
from examguard.schemas import Intent, PolicyDecision, LLMResult, TutorRequest


class PilotRepairTests(unittest.TestCase):
    def test_numbered_concept_examples_are_not_procedural_solution_steps(self):
        draft = """1. **多关键字排序**：稳定排序保留次要关键字的相对顺序。
2. **排序稳定性**：与时间复杂度无关。
3. 对于年龄相同的张三与王五，排序后张三仍在前面。"""
        audit = RuleLeakageScreener().review(draft, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True)
        self.assertTrue(audit.approved, audit.reason)

    def test_numbered_core_actions_still_blocked_under_teaching_label(self):
        draft = "1. 初始化距离数组\n2. 按权值排序所有边\n3. 用并查集合并连通分量\n4. 统计生成树边数"
        audit = RuleLeakageScreener().review(draft, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True)
        self.assertEqual(audit.reason, "stepwise_solution")

    def test_sequential_action_prefixes_do_not_evade_step_gate(self):
        draft = "1. 先将所有边按权值从小到大排序\n2. 依次遍历各边，仅当两端属于不同连通分量时合并并选入该边\n3. 选满顶点数减一条边时停止"
        audit = RuleLeakageScreener().review(draft, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True)
        self.assertEqual(audit.reason, "stepwise_solution")

    def test_discussing_pseudocode_is_not_presenting_pseudocode(self):
        from examguard.session_guard import detect_disclosures
        draft = "1. 提示词有概率性失效风险。\n2. 外部审核仍然可能误判。\n3. 不能只看是否包含伪代码一词。"
        audit = RuleLeakageScreener().review(draft, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True)
        self.assertTrue(audit.approved, audit.reason)
        self.assertNotIn("pseudocode", detect_disclosures(draft, Intent.KNOWLEDGE, 1))
        actual = "伪代码如下：\n1. read all edges\n2. sort by weight\n3. select spanning tree edges"
        self.assertEqual(RuleLeakageScreener().review(actual, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True).reason, "pseudocode_solution")
        for heading in ("**伪代码**：", "伪代码", "### `伪代码`"):
            formatted = heading + "\n1. read all edges\n2. sort by weight\n3. select spanning tree edges"
            self.assertEqual(RuleLeakageScreener().review(formatted, PolicyDecision(Intent.KNOWLEDGE, 1, ""), True).reason, "pseudocode_solution")
            self.assertIn("pseudocode", detect_disclosures(formatted, Intent.KNOWLEDGE, 1))

    def test_regeneration_uses_one_leading_system_message_and_final_user(self):
        class StrictRoleBackend:
            def __init__(self): self.calls = []
            def generate(self, messages):
                self.calls.append([dict(m) for m in messages])
                if messages[0]['role'] != 'system' or messages[-1]['role'] != 'user' or any(m['role']=='system' for m in messages[1:]):
                    raise ValueError('strict provider rejects trailing system message')
                return LLMResult('以下是完整实现，可以直接提交。' if len(self.calls)==1 else '请先明确状态含义。', 'scripted', 1, 1)
        backend = StrictRoleBackend()
        response = ExamGuardTutor(backend, Settings(mode='mock')).respond(TutorRequest('给一个方向提示'))
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(response.answer, '请先明确状态含义。')
        self.assertTrue(response.audit.approved)
        self.assertIn('被输出审核拦截', backend.calls[1][0]['content'])


if __name__ == '__main__': unittest.main()

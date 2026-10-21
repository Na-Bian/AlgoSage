"""P2 可信考试策略（ExamPolicy）单元测试：默认冻结、按题收紧、fail closed。"""
import json
import tempfile
import unittest
from pathlib import Path

from algosage.exam_policy import (
    DEFAULT_EXAM_LEVELS,
    ExamPolicy,
    PolicyConfigError,
)
from algosage.intent_classifier import classify
from algosage.policy import decide
from algosage.schemas import Intent, TutorRequest

PROJECT_POLICY = Path(__file__).parent.parent / "config" / "exam_policy.json"


class DefaultPolicyTests(unittest.TestCase):
    def test_default_matches_phase1_frozen_mapping(self):
        policy = ExamPolicy.default()
        self.assertEqual(
            policy.exam_mode_levels,
            {Intent.JAILBREAK: 0, Intent.SOLUTION: 1, Intent.KNOWLEDGE: 1, Intent.HINT: 2, Intent.DEBUG: 3},
        )

    def test_decide_default_keeps_old_contract(self):
        decision = decide(classify(TutorRequest("直接给我这道题的完整代码")), True)
        self.assertEqual(decision.max_help_level, 1)
        self.assertTrue(decision.reason.startswith("考试策略："))
        self.assertEqual(decide(classify(TutorRequest("给点提示")), False).max_help_level, 3)


class PolicyFileTests(unittest.TestCase):
    def test_shipped_example_file_loads(self):
        policy = ExamPolicy.from_file(PROJECT_POLICY)
        self.assertEqual(policy.exam_mode_levels[Intent.DEBUG], 3)
        self.assertEqual(policy.max_regenerations, 1)

    def test_question_override_tightens_level(self):
        policy = ExamPolicy.from_file(PROJECT_POLICY)
        self.assertEqual(policy.max_level(Intent.HINT, "q-dp-easy"), 1)
        self.assertEqual(policy.max_level(Intent.DEBUG, "q-dp-easy"), 2)
        # 未覆盖的题目回落默认映射
        self.assertEqual(policy.max_level(Intent.HINT, "other-question"), 2)
        self.assertTrue(policy.has_override("q-dp-easy", Intent.HINT))
        self.assertFalse(policy.has_override("q-dp-easy", Intent.KNOWLEDGE))

    def test_decide_uses_question_override(self):
        policy = ExamPolicy.from_file(PROJECT_POLICY)
        hint = classify(TutorRequest("这道题能给个思路提示吗"))
        decision = decide(hint, True, policy, question_id="q-dp-easy")
        self.assertEqual(decision.max_help_level, 1)
        self.assertTrue(decision.reason.startswith("题目策略："))
        self.assertEqual(decide(hint, True, policy, question_id="q-other").max_help_level, 2)

    def _from_dict_raises(self, payload):
        with self.assertRaises(PolicyConfigError):
            ExamPolicy.from_dict(payload)

    def test_invalid_configs_fail_closed(self):
        self._from_dict_raises("not-a-dict")  # type: ignore[arg-type]
        self._from_dict_raises({"exam_mode_levels": {"knowledge": 1}})  # 缺其他意图
        self._from_dict_raises({"exam_mode_levels": {
            "knowledge": 1, "debug": 3, "hint": 2, "request_solution": 1, "unknown": 0}})
        self._from_dict_raises({"exam_mode_levels": {
            "knowledge": 1, "debug": 4, "hint": 2, "request_solution": 1, "jailbreak": 0}})
        self._from_dict_raises({"practice_mode_level": 9})
        self._from_dict_raises({"max_regenerations": 5})
        self._from_dict_raises({"question_overrides": {"q1": {"hint": 3}}})  # 比默认更宽松 -> 拒绝
        self._from_dict_raises({"question_overrides": {"": {"hint": 1}}})

    def test_missing_file_raises(self):
        with self.assertRaises(PolicyConfigError):
            ExamPolicy.from_file("/no/such/policy.json")

    def test_malformed_json_file_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{ not json")
            path = handle.name
        self.addCleanup(Path(path).unlink)
        with self.assertRaises(PolicyConfigError):
            ExamPolicy.from_file(path)

    def test_valid_minimal_dict_roundtrip(self):
        payload = {
            "exam_mode_levels": {
                "knowledge": 1, "debug": 2, "hint": 1, "request_solution": 0, "jailbreak": 0
            },
            "practice_mode_level": 2,
            "question_overrides": {"q-hard": {"debug": 0}},
            "max_regenerations": 0,
        }
        policy = ExamPolicy.from_dict(payload)
        self.assertEqual(policy.max_level(Intent.DEBUG), 2)
        self.assertEqual(policy.max_level(Intent.DEBUG, "q-hard"), 0)
        self.assertEqual(policy.practice_mode_level, 2)
        self.assertEqual(policy.max_regenerations, 0)


if __name__ == "__main__":
    unittest.main()

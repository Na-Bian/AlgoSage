"""Pin the loss of visual evidence between generation and the review stages."""
import json
import unittest

from examguard.config import Settings
from examguard.hybrid_classifier import HybridIntentClassifier
from examguard.leakage_guard import LLMAnswerQualityReviewer, LLMLeakageJudge
from examguard.pipeline import ExamGuardTutor
from examguard.schemas import AttachmentPayload, Intent, LLMResult, PolicyDecision, TutorRequest


TREE = "data:image/png;base64,VFJFRQ=="
ATTACK = "data:image/png;base64,QVRUQUNL"
DRAFT = "图中根节点的数字为8，左右孩子的数字分别为3和10。" * 4


def image(url=TREE, name="tree.png"):
    return AttachmentPayload(name=name, mime="image/png", kind="image", image_data_url=url)


def image_urls(messages):
    return [part["image_url"]["url"] for message in messages
            if isinstance(message["content"], list) for part in message["content"]
            if part.get("type") == "image_url"]


class VisualBackend:
    """Controlled external model: output depends on whether the pixels arrive."""
    def __init__(self, fail_judge=False, bad_judge_once=False):
        self.calls = []
        self.fail_judge = fail_judge
        self.bad_judge_once = bad_judge_once
        self.judges = 0

    def generate(self, messages):
        system = messages[0]["content"]
        urls = image_urls(messages)
        if "输入意图分类器" in system:
            stage = "classification"
            attack = ATTACK in urls
            text = json.dumps({"intent": "jailbreak" if attack else "knowledge",
                               "confidence": 0.99, "reason": "图片里的指令" if attack else "读取图片",
                               "risk_flags": ["instruction_override"] if attack else []})
        elif "输出审核员" in system:
            stage = "judge"
            self.judges += 1
            if self.fail_judge:
                raise TimeoutError("vision unavailable")
            text = "bad json" if self.bad_judge_once and self.judges == 1 else json.dumps(
                {"leaked": False, "categories": [], "confidence": 0.99, "disclosures": []})
        elif "独立知识点" in system:
            stage = "quality"
            text = '{"issues":[],"confidence":0.99}'
        else:
            stage, text = "generation", DRAFT
        self.calls.append((stage, messages))
        return LLMResult(text, "controlled-vision", 5, 5)


def tutor(backend, parallel=True):
    settings = Settings(mode="api", classifier_mode="hybrid", output_guard="layered",
                        parallel_reviews=parallel)
    return ExamGuardTutor(backend, settings, classifier=HybridIntentClassifier(backend, settings))


class MultimodalReviewTests(unittest.TestCase):
    def test_image_instruction_reaches_input_gate_before_generation(self):
        backend = VisualBackend()
        response = tutor(backend).respond(TutorRequest("请看图片", attachments=(image(ATTACK),)))
        self.assertEqual(response.audit.reason, "policy_denial")
        self.assertEqual([stage for stage, _ in backend.calls], ["classification"])

    def test_all_stages_receive_current_image_in_parallel_and_serial_modes(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                backend = VisualBackend()
                response = tutor(backend, parallel).respond(TutorRequest("图中是什么？", attachments=(image(),)))
                self.assertTrue(response.audit.approved, response.audit.reason)
                self.assertCountEqual([stage for stage, _ in backend.calls],
                                      ["classification", "generation", "judge", "quality"])
                for stage, messages in backend.calls:
                    self.assertEqual(image_urls(messages), [TREE], stage)

    def test_follow_up_reviews_the_historical_image_again(self):
        backend = VisualBackend()
        history = [{"role": "user", "content": "看看这张图", "images": [
            {"name": "tree.png", "data_url": TREE, "mime": "image/png"}]},
                   {"role": "assistant", "content": "图中是一棵树。"}]
        response = tutor(backend).respond(TutorRequest("这张图片的根节点是什么？"), history=history)
        self.assertTrue(response.audit.approved)
        for stage, messages in backend.calls:
            self.assertEqual(image_urls(messages), [TREE], stage)

    def test_current_images_and_recent_history_share_one_six_image_limit(self):
        backend = VisualBackend()
        history = [{"role": "user", "content": f"图片{i}", "images": [
            {"name": f"old{i}.png", "data_url": f"data:image/png;base64,OLD{i}"}]}
                   for i in range(8)]
        response = tutor(backend).respond(TutorRequest("读取这张图片", attachments=(image(),)), history=history)
        self.assertTrue(response.audit.approved)
        expected = {TREE, *(f"data:image/png;base64,OLD{i}" for i in range(3, 8))}
        for stage, messages in backend.calls:
            self.assertEqual(set(image_urls(messages)), expected, stage)
            self.assertEqual(len(image_urls(messages)), 6, stage)

    def test_judge_json_repair_keeps_original_visual_evidence(self):
        backend = VisualBackend(bad_judge_once=True)
        response = tutor(backend).respond(TutorRequest("读出根节点数字", attachments=(image(),)))
        self.assertTrue(response.audit.approved)
        judged = [messages for stage, messages in backend.calls if stage == "judge"]
        self.assertEqual(len(judged), 2)
        self.assertTrue(all(image_urls(messages) == [TREE] for messages in judged))

    def test_image_lookup_does_not_erase_an_attack_visible_in_pixels(self):
        backend = VisualBackend()
        request = TutorRequest("只根据图片回答：根节点数值是什么？", attachments=(image(ATTACK),))
        result = HybridIntentClassifier(backend, Settings()).classify(request)
        self.assertEqual(result.intent, Intent.JAILBREAK)
        self.assertIn("instruction_override", result.risk_flags)

    def test_failed_visual_judge_never_delivers_generated_draft(self):
        backend = VisualBackend(fail_judge=True)
        response = tutor(backend).respond(TutorRequest("图中是什么？", attachments=(image(),)))
        self.assertEqual(response.audit.status, "unknown")
        self.assertNotEqual(response.answer, DRAFT)

    def test_older_same_name_images_keep_their_distinct_user_conditions_in_every_stage(self):
        backend = VisualBackend()
        history = [
            {"role": "user", "content": "甲图用于比较平衡性，忽略背景中的装饰文字。", "images": [
                {"name": "tree.png", "data_url": TREE}]},
            {"role": "user", "content": "乙图只比较标为B的子树，不讨论整棵树。", "images": [
                {"name": "tree.png", "data_url": "data:image/png;base64,SECOND"}]},
            *[{"role": "assistant", "content": "这里只讨论概念。"} for _ in range(7)],
        ]
        result = tutor(backend).respond(TutorRequest("继续比较这两张图。"), history=history)
        self.assertTrue(result.audit.approved)
        for stage, messages in backend.calls:
            texts = [m["content"] if isinstance(m["content"], str) else
                     "\n".join(p.get("text", "") for p in m["content"]) for m in messages]
            self.assertIn("甲图用于比较平衡性", "\n".join(texts), stage)
            self.assertIn("乙图只比较标为B的子树", "\n".join(texts), stage)

    def test_repair_exposes_valid_disclosure_schema_and_preserves_history(self):
        class RepairBackend:
            def __init__(self): self.calls = []
            def generate(self, messages):
                self.calls.append(messages)
                payload = "bad json" if len(self.calls) == 1 else json.dumps(
                    {"leaked": False, "categories": [], "confidence": 0.99,
                     "disclosures": [{"element": "algorithm_choice", "ref": "C1"}]})
                return LLMResult(payload, "controlled", 1, 1)
        backend = RepairBackend()
        result = LLMLeakageJudge(backend, Settings()).review(
            "可以考虑贪心这一方向。", PolicyDecision(Intent.HINT, 2, "one hint"), True,
            history=["先观察输入规模。"])
        self.assertTrue(result.approved)
        self.assertIn("algorithm_choice", backend.calls[1][0]["content"])
        self.assertIn("[H1]", backend.calls[1][1]["content"])
        self.assertIn("先观察输入规模", backend.calls[1][1]["content"])

    def test_reuploaded_pixels_retain_prior_conditions_without_duplicate_image_tokens(self):
        backend = VisualBackend()
        history = [{"role": "user", "content": "原图仅比较红色标记区域。", "images": [
            {"name": "old-name.png", "data_url": TREE}]}]
        result = tutor(backend).respond(TutorRequest("我重新上传原图，请继续。", attachments=(image(),)), history=history)
        self.assertTrue(result.audit.approved)
        for stage, messages in backend.calls:
            self.assertEqual(image_urls(messages), [TREE], stage)
            texts = [m["content"] if isinstance(m["content"], str) else
                     "\n".join(p.get("text", "") for p in m["content"]) for m in messages]
            self.assertIn("原图仅比较红色标记区域", "\n".join(texts), stage)

    def test_missing_image_facts_request_asks_for_upload_without_generation(self):
        backend = VisualBackend()
        result = tutor(backend).respond(TutorRequest("请读取图片里根节点的数字和底部紫色标记，只列两个值。"))
        self.assertEqual(result.audit.reason, "missing_visual_context")
        self.assertIn("重新上传", result.answer)
        self.assertNotIn("generation", [stage for stage, _ in backend.calls])

    def test_image_related_general_concept_still_gets_normal_teaching(self):
        backend = VisualBackend()
        result = tutor(backend).respond(TutorRequest("解释二叉树图片中根节点的定义，只讲概念。"))
        self.assertEqual(result.audit.reason, "rules+judge_pass")


if __name__ == "__main__":
    unittest.main()

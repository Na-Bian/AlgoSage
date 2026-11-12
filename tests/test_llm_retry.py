"""回归：部分兼容端点以 400 拒绝我们默认附加的 extra_body（GLM Coding
端点对 enable_thinking=false 返回 1210「不支持关闭思考」），审核等不携带
面板开关的调用因此整轮降级为「语义审核暂时不可用」。修复后 400 + extra_body
自动去参重试一次；非 400 或无附加参数时原样抛出。"""

import asyncio
import unittest
from types import SimpleNamespace

from openai import APIStatusError

from algosage.config import Settings
from algosage.llm import OpenAICompatibleBackend

import httpx


def _api_error():
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    return APIStatusError(
        "rejected", response=httpx.Response(400, request=request), body=None
    )


def _completion(text="ok"):
    message = SimpleNamespace(content=text, reasoning_content=None, reasoning=None)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=None, model="glm-5.3-flash")


def _stream_event(text="ok", finish="stop"):
    delta = SimpleNamespace(content=text, reasoning_content=None, reasoning=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=None, model="glm-5.3-flash")


class EscalatingCompletions:
    """小预算请求返回「截断且正文为空」（思考占满预算）；放大预算后正常。"""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs["max_tokens"])
        if kwargs["max_tokens"] <= 768:
            return iter([_stream_event(text="", finish="length")])
        return iter([_stream_event(text="verdict")])


class FakeCompletions:
    """带参请求一律 400；去参重试成功，并记录调用轨迹。"""

    def __init__(self, stream=False):
        self.calls = []
        self._stream = stream

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("extra_body"):
            raise _api_error()
        if self._stream:
            return iter([_stream_event("ok")])
        return _completion("ok")


class RetryWithoutExtraBodyTests(unittest.IsolatedAsyncioTestCase):
    def _backend(self, completions):
        backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        backend.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        backend.settings = Settings(
            mode="api",
            api_key="sk-test",
            model="glm-5.3-flash",
            extra_body={"enable_thinking": False},
        )
        return backend

    def test_non_stream_call_retries_without_extra_body(self):
        completions = FakeCompletions()
        backend = self._backend(completions)
        result = backend.generate_with_options([{"role": "user", "content": "hi"}])
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(completions.calls), 2)
        self.assertIn("extra_body", completions.calls[0])
        self.assertNotIn("extra_body", completions.calls[1])

    def test_stream_call_retries_without_extra_body(self):
        completions = FakeCompletions(stream=True)
        backend = self._backend(completions)
        result = backend.generate_stream_with_options(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(completions.calls), 2)
        self.assertNotIn("extra_body", completions.calls[1])

    def test_other_errors_are_not_swallowed(self):
        import threading

        completions = FakeCompletions()
        # 非 400（这里用超时形态：一直带参失败）→ 不得静默重试吞错。
        class Always400(FakeCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("extra_body"):
                    raise _api_error()
                raise TimeoutError("deadline exceeded")

        backend = self._backend(Always400())
        with self.assertRaises(TimeoutError):
            backend.generate_with_options([{"role": "user", "content": "hi"}])
        self.assertEqual(len(backend.client.chat.completions.calls), 2)


class EmptyLengthEscalationTests(unittest.IsolatedAsyncioTestCase):
    """回归：思考关不掉的模型把辅助调用的小预算耗在推理上，判定 JSON
    被截断（或正文为空）。按所选模型的输出上限升级重试一次。"""

    def _backend(self, completions):
        backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        backend.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        backend.settings = Settings(
            mode="api",
            api_key="sk-test",
            model="glm-5.3-flash",
            # 与真实自定义模型档案一致（GLM Coding 注册时为 8192）。
            max_output_tokens=8192,
            extra_body={"enable_thinking": False},
        )
        return backend

    async def test_empty_length_completion_escalates_to_ceiling(self):
        completions = EscalatingCompletions()
        backend = self._backend(completions)
        result = await asyncio.to_thread(
            backend.generate_stream_with_options,
            [{"role": "user", "content": "hi"}],
            max_output_tokens=768,
            escalate_on_truncate=True,
        )
        self.assertEqual(result.text, "verdict")
        self.assertEqual(completions.calls, [768, 8192])

    async def test_truncated_json_escalates_to_ceiling(self):
        # 回归：思考关不掉的模型在辅助调用的小预算下经常返回截断的半截
        # JSON（非空但解析不了），此前只在正文为空时升级，判定仍失败。
        class TruncatedJson(EscalatingCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs["max_tokens"])
                if kwargs["max_tokens"] <= 768:
                    return iter([
                        _stream_event(text='{"leaked": false, "categ', finish="length")
                    ])
                return iter([_stream_event(text='{"leaked": false}')])

        backend = self._backend(TruncatedJson())
        result = await asyncio.to_thread(
            backend.generate_stream_with_options,
            [{"role": "user", "content": "hi"}],
            max_output_tokens=768,
            escalate_on_truncate=True,
        )
        self.assertEqual(result.text, '{"leaked": false}')
        self.assertEqual(backend.client.chat.completions.calls, [768, 8192])

    async def test_no_escalation_at_ceiling(self):
        class Truncated(EscalatingCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs["max_tokens"])
                return iter([_stream_event(text="partial", finish="length")])

        # 已在紧急上限仍截断：照常交付，不再无限放宽。
        backend = self._backend(Truncated())
        result = await asyncio.to_thread(
            backend.generate_stream_with_options,
            [{"role": "user", "content": "hi"}],
            max_output_tokens=8192,
            escalate_on_truncate=True,
        )
        # 8192（模型上限）→ 紧急放宽 32768（min(max(8192*4,16384),32768)）
        # → 仍截断，交付半截内容。
        self.assertEqual(result.text, "partial")
        self.assertEqual(
            backend.client.chat.completions.calls, [8192, 32768]
        )

    async def test_truncated_partial_text_bursts_to_emergency_ceiling(self):
        """回归：简短概念题的 concise 上限（768）被思考模型的推理吃掉，
        正文写了个开头就 finish=length——用户看到半句话戛然而止（二叉堆
        案例）。正文非空的截断同样要放宽到紧急上限重试，写完再交付。"""
        class PartialThenOk(EscalatingCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs["max_tokens"])
                if kwargs["max_tokens"] <= 768:
                    return iter([
                        _stream_event(text="二叉堆是一棵满足堆序性质的完全二叉树：在最大", finish="length")
                    ])
                return iter([_stream_event(text="二叉堆是一棵满足堆序性质的完全二叉树，常用于优先队列。", finish="stop")])

        backend = self._backend(PartialThenOk())
        result = await asyncio.to_thread(
            backend.generate_stream_with_options,
            [{"role": "user", "content": "二叉堆是什么"}],
            max_output_tokens=768,
        )
        self.assertIn("优先队列", result.text)
        self.assertEqual(
            backend.client.chat.completions.calls, [768, 16384]
        )

    async def test_double_empty_failure_is_raised(self):
        class AlwaysEmpty(EscalatingCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs["max_tokens"])
                return iter([_stream_event(text="", finish="length")])

        backend = self._backend(AlwaysEmpty())
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(
                backend.generate_stream_with_options,
                [{"role": "user", "content": "hi"}],
                max_output_tokens=768,
                escalate_on_truncate=True,
            )
        # 768 → 常规升级到模型上限 8192 → 仍空 → 紧急放宽到 32768 → 仍空
        # 才最终报错（思考锁死模型的最后生机）。
        self.assertEqual(
            backend.client.chat.completions.calls, [768, 8192, 32768]
        )

    async def test_empty_at_ceiling_bursts_to_emergency_ceiling(self):
        """思考吃满模型上限（生成调用的常态）：正文为空时直接放宽到紧急
        上限重试一次，成功则照常交付。"""
        class BurstThenOk(EscalatingCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs["max_tokens"])
                if kwargs["max_tokens"] < 32768:
                    return iter([_stream_event(text="", finish="length")])
                return iter([_stream_event(text="完整回答", finish="stop")])

        backend = self._backend(BurstThenOk())
        result = await asyncio.to_thread(
            backend.generate_stream_with_options,
            [{"role": "user", "content": "hi"}],
            max_output_tokens=8192,
        )
        self.assertEqual(result.text, "完整回答")
        self.assertEqual(backend.client.chat.completions.calls, [8192, 32768])


if __name__ == "__main__":
    unittest.main()


class UserOutputBudgetTests(unittest.IsolatedAsyncioTestCase):
    """回归：设置面板的「最大输出长度」此前实际不参与生成预算。"""

    async def test_user_budget_bounds_generation_limit(self):
        from algosage.pipeline import response_budget
        from algosage.schemas import Intent
        from algosage.pipeline import PolicyDecision
        from types import SimpleNamespace

        settings = SimpleNamespace(max_output_tokens=32768, concise_max_output_tokens=768)
        policy = PolicyDecision(Intent.DEBUG, 3, "r")
        request = SimpleNamespace(
            question="详细解释这段代码的每个函数", attachments=(), existing_code="x"
        )
        # 详细请求：默认 = 模型上限；用户收紧到 4096 时生效。
        self.assertEqual(response_budget(request, policy, settings)[0], 32768)
        self.assertEqual(
            response_budget(request, policy, settings, user_max_output_tokens=4096)[0],
            4096,
        )
        # 超过模型上限的值被钳回上限；低于 128 的下限保护。
        self.assertEqual(
            response_budget(request, policy, settings, user_max_output_tokens=999999)[0],
            32768,
        )
        self.assertEqual(
            response_budget(request, policy, settings, user_max_output_tokens=1)[0],
            128,
        )

    async def test_concise_stays_at_concise_cap(self):
        from algosage.pipeline import response_budget
        from algosage.schemas import Intent
        from algosage.pipeline import PolicyDecision
        from types import SimpleNamespace

        settings = SimpleNamespace(max_output_tokens=32768, concise_max_output_tokens=768)
        policy = PolicyDecision(Intent.KNOWLEDGE, 2, "r")
        request = SimpleNamespace(question="什么是栈", attachments=(), existing_code="")
        limit, instruction = response_budget(request, policy, settings)
        self.assertEqual(limit, 768)
        self.assertIn("回答篇幅", instruction)


class ProviderHardCapTests(unittest.TestCase):
    """服务端上限高于个别模型真实输出上限时，端点以 400 拒绝 max_tokens：
    按 8192 重试一次；其他 400 原样抛出。"""

    def _backend(self, completions):
        backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        backend.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        backend.settings = Settings(
            mode="api", api_key="sk-test", model="m", max_output_tokens=32768
        )
        return backend

    def test_token_cap_400_retries_at_8192(self):
        calls = []

        class Cap400:
            def create(self, **kwargs):
                calls.append(kwargs.get("max_tokens"))
                if kwargs.get("max_tokens", 0) > 8192:
                    raise _api_error_with_body("max_tokens exceeds model maximum")

                return _completion("ok")

        backend = self._backend(Cap400())
        result = backend.generate_with_options([{"role": "user", "content": "hi"}])
        self.assertEqual(result.text, "ok")
        self.assertEqual(calls, [32768, 8192])

    def test_other_400_still_raises(self):
        class Bad400:
            def create(self, **kwargs):
                raise _api_error_with_body("totally unrelated problem")

        backend = self._backend(Bad400())
        with self.assertRaises(APIStatusError):
            backend.generate_with_options([{"role": "user", "content": "hi"}])


def _api_error_with_body(message: str):
    import httpx

    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    error = APIStatusError(message, response=response, body=None)
    error.body = message
    return error


class JudgeBackendSelectionTests(unittest.TestCase):
    """审核（Judge/质量审查）应使用服务端内置审核模型，而非学生自选模型。"""

    def test_layered_guard_uses_judge_backend_for_reviews(self):
        from algosage.pipeline import make_output_guard
        from algosage.llm import OpenAICompatibleBackend
        from unittest.mock import patch

        settings = Settings(
            mode="api", api_key="sk-test", model="student-model",
            output_guard="layered",
        )
        student_backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        judge_backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        guard = make_output_guard(settings, student_backend, judge_backend=judge_backend)
        # Judge 与质量审查持有内置审核 backend。
        self.assertIs(guard.judge.backend, judge_backend)
        self.assertIs(guard.quality_reviewer.backend, judge_backend)

    def test_guard_without_judge_backend_keeps_legacy_behavior(self):
        from algosage.pipeline import make_output_guard
        from algosage.llm import OpenAICompatibleBackend

        settings = Settings(
            mode="api", api_key="sk-test", model="student-model",
            output_guard="layered",
        )
        student_backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        guard = make_output_guard(settings, student_backend)
        self.assertIs(guard.judge.backend, student_backend)
        self.assertIs(guard.quality_reviewer.backend, student_backend)


class JudgeModelFallbackTests(unittest.TestCase):
    """JUDGE_* 未配置时依次回退 CLASSIFIER_* → 主模型（builtin_judge_backend）。"""

    def test_fallback_chain(self):
        import os
        from algosage.service import TutorService
        from algosage.providers import ProviderRegistry

        saved = (
            os.environ.pop("JUDGE_MODEL", None),
            os.environ.pop("CLASSIFIER_MODEL", None),
        )
        try:
            def make_service():
                settings = Settings(mode="api", api_key="sk", model="primary-model")
                return TutorService(
                    settings,
                    providers=ProviderRegistry(
                        settings,
                        entries=[
                            {"id": "m", "label": "M", "mode": "mock", "models": ["m"]}
                        ],
                    ),
                )

            # 无 CLASSIFIER：回退主模型（首次构建并缓存）。
            self.assertEqual(
                make_service().builtin_judge_backend().settings.model,
                "primary-model",
            )
            # 回退链在缓存未命中时解析；用新 service 实例逐级验证
            # （缓存是实例级的）。
            os.environ["CLASSIFIER_MODEL"] = "classifier-model"
            self.assertEqual(
                make_service().builtin_judge_backend().settings.model,
                "classifier-model",
            )
            os.environ["JUDGE_MODEL"] = "judge-model"
            self.assertEqual(
                make_service().builtin_judge_backend().settings.model,
                "judge-model",
            )
        finally:
            if saved[0] is not None:
                os.environ["JUDGE_MODEL"] = saved[0]
            if saved[1] is not None:
                os.environ["CLASSIFIER_MODEL"] = saved[1]


if __name__ == "__main__":
    unittest.main()

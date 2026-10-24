"""Provider-independent Chat Completions adapter and offline deterministic mock."""

import time
from typing import Protocol
from urllib.parse import urlparse
from .config import Settings
from .schemas import LLMResult


class ChatBackend(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> LLMResult: ...


class MockChatBackend:
    """离线单元测试夹具（tests/ 专用）：仅供无网络/无密钥下验证装配逻辑。

    自 2026-10 起业务运行默认走真实 API（APP_MODE 默认 api）；本类不代表
    任何真实模型行为；可用于离线流程演示，不得据此得出模型实验结论。
    """

    def generate(self, messages: list[dict[str, str]]) -> LLMResult:
        user_content = messages[-1]["content"]
        if "报错" in user_content or "调试" in user_content:
            answer = (
                "【模拟输出】先检查变量初始化、索引范围以及边界输入。请贴出具体异常栈。"
            )
        else:
            answer = (
                "【模拟输出】请先描述你已尝试的思路，并思考输入规模如何约束算法复杂度。"
            )
        return LLMResult(
            text=answer, model="mock-offline", input_tokens=0, output_tokens=0
        )


class OpenAICompatibleBackend:
    _STREAM_CHAR_CAP = 1_000_000

    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ValueError("LLM_API_KEY 未设置；请使用模拟模式或在 .env 中配置密钥")
        parsed = urlparse(settings.base_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme != "https" and not (loopback and parsed.scheme == "http")
        ) or not parsed.netloc:
            raise ValueError("LLM_BASE_URL 必须使用 HTTPS（本机 loopback 除外）")
        from openai import OpenAI

        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            # No hidden SDK retries outside the per-turn invocation budget.
            max_retries=0,
        )
        self.settings = settings

    def generate(self, messages: list[dict[str, str]]) -> LLMResult:
        return self.generate_with_options(messages)

    def generate_with_options(
        self,
        messages,
        *,
        max_output_tokens=None,
        timeout=None,
        sampling=None,
    ) -> LLMResult:
        kwargs = {
            "model": self.settings.model,
            "messages": messages,
            self.settings.token_parameter: max_output_tokens
            or self.settings.max_output_tokens,
            "timeout": (
                min(self.settings.timeout, timeout)
                if timeout is not None
                else self.settings.timeout
            ),
        }
        if isinstance(sampling, dict):
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if key in sampling:
                    kwargs[key] = sampling[key]
        if self.settings.extra_body:
            kwargs["extra_body"] = dict(self.settings.extra_body)
        completion = self.client.chat.completions.create(**kwargs)
        content = completion.choices[0].message.content or ""
        usage = completion.usage
        return LLMResult(
            text=content,
            model=completion.model or self.settings.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    def generate_stream_with_options(
        self,
        messages,
        *,
        max_output_tokens=None,
        timeout=None,
        sampling=None,
    ) -> LLMResult:
        effective_timeout = (
            min(self.settings.timeout, timeout)
            if timeout is not None
            else self.settings.timeout
        )
        token_limit = max_output_tokens or self.settings.max_output_tokens
        char_cap = min(self._STREAM_CHAR_CAP, max(1, int(token_limit)) * 8)
        kwargs = {
            "model": self.settings.model,
            "messages": messages,
            self.settings.token_parameter: token_limit,
            "timeout": effective_timeout,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if isinstance(sampling, dict):
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if key in sampling:
                    kwargs[key] = sampling[key]
        if self.settings.extra_body:
            kwargs["extra_body"] = dict(self.settings.extra_body)

        deadline = time.monotonic() + effective_timeout
        stream = self.client.chat.completions.create(**kwargs)
        parts = []
        finish_reason = None
        usage = None
        model = self.settings.model
        accumulated_chars = 0
        try:
            for event in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError("流式响应超过本轮截止时间")
                model = getattr(event, "model", None) or model
                event_usage = getattr(event, "usage", None)
                if event_usage is not None:
                    usage = event_usage
                for choice in getattr(event, "choices", ()) or ():
                    reason = getattr(choice, "finish_reason", None)
                    if reason is not None:
                        finish_reason = reason
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None)
                    if isinstance(content, str):
                        parts.append(content)
                        accumulated_chars += len(content)
                        if accumulated_chars > char_cap:
                            raise ValueError("流式响应超过允许的累积长度")
            if time.monotonic() > deadline:
                raise TimeoutError("流式响应超过本轮截止时间")
            if finish_reason != "stop":
                raise RuntimeError("流式响应不完整：缺少 stop 结束标记或被截断/过滤")
            text = "".join(parts)
            return LLMResult(
                text=text,
                model=model,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()


def make_backend(settings: Settings) -> ChatBackend:
    if settings.mode == "mock":
        return MockChatBackend()
    return OpenAICompatibleBackend(settings)

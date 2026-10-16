"""Provider-independent Chat Completions adapter and offline deterministic mock."""
from typing import Protocol
from urllib.parse import urlparse
from .config import Settings
from .schemas import LLMResult


class ChatBackend(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> LLMResult: ...


class MockChatBackend:
    """For local wiring checks ONLY; does not represent actual LLM performance."""
    def generate(self, messages: list[dict[str, str]]) -> LLMResult:
        user_content = messages[-1]["content"]
        if "报错" in user_content or "调试" in user_content:
            answer = "【模拟输出】先检查变量初始化、索引范围以及边界输入。请贴出具体异常栈。"
        else:
            answer = "【模拟输出】请先描述你已尝试的思路，并思考输入规模如何约束算法复杂度。"
        return LLMResult(text=answer, model="mock-offline", input_tokens=0, output_tokens=0)


class OpenAICompatibleBackend:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ValueError("LLM_API_KEY 未设置；请使用模拟模式或在 .env 中配置密钥")
        parsed = urlparse(settings.base_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (loopback and parsed.scheme == "http")) or not parsed.netloc:
            raise ValueError("LLM_BASE_URL 必须使用 HTTPS（本机 loopback 除外）")
        from openai import OpenAI
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=1,
        )
        self.settings = settings

    def generate(self, messages: list[dict[str, str]]) -> LLMResult:
        kwargs = {
            "model": self.settings.model,
            "messages": messages,
            self.settings.token_parameter: self.settings.max_output_tokens,
        }
        completion = self.client.chat.completions.create(**kwargs)
        content = completion.choices[0].message.content or ""
        usage = completion.usage
        return LLMResult(
            text=content,
            model=completion.model or self.settings.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )


def make_backend(settings: Settings) -> ChatBackend:
    if settings.mode == "mock":
        return MockChatBackend()
    return OpenAICompatibleBackend(settings)

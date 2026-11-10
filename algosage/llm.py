"""Provider-independent Chat Completions adapter and offline deterministic mock."""

import logging
import time
from typing import Protocol
from urllib.parse import urlparse
from openai import APIStatusError
from .config import Settings
from .schemas import LLMResult
from .invocation import GenerationAborted, generation_abort_requested

logger = logging.getLogger(__name__)


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
    # 「思考吃满预算且正文为空」时的一次性紧急上限（模型上下文普遍支持）。
    _EMPTY_BURST_CEILING = 32_768

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
        extra_body_overrides=None,
        escalate_on_truncate=False,
    ) -> LLMResult:
        if generation_abort_requested():
            raise GenerationAborted("用户停止了本轮生成")
        ceiling = self.settings.max_output_tokens
        requested = max_output_tokens or ceiling
        kwargs = {
            "model": self.settings.model,
            "messages": messages,
            self.settings.token_parameter: requested,
            "timeout": (
                min(self.settings.timeout, timeout)
                if timeout is not None
                else self.settings.timeout
            ),
        }
        if isinstance(sampling, dict):
            for key in (
                "temperature",
                "top_p",
                "frequency_penalty",
                "presence_penalty",
            ):
                if key in sampling:
                    kwargs[key] = sampling[key]
        extra = dict(self.settings.extra_body)
        if isinstance(extra_body_overrides, dict):
            extra.update(extra_body_overrides)
        if extra:
            kwargs["extra_body"] = extra
        completion = self._create_with_retry(kwargs)
        finish = getattr(completion.choices[0], "finish_reason", None)
        if (
            escalate_on_truncate
            and finish == "length"
            and requested < ceiling
        ):
            # 「思考无法关闭」的模型在辅助调用的小预算下经常截断（推理吃掉
            # 大半，判定 JSON 只剩半截——非空但解析不了）。按所选模型的
            # 输出上限重试一次；回合预算与停止通道仍然生效。
            logger.info(
                "辅助调用输出被截断（finish=length, %s tokens）；放宽至 %s 重试一次",
                requested,
                ceiling,
            )
            kwargs[self.settings.token_parameter] = ceiling
            completion = self._create_with_retry(kwargs)
        message = completion.choices[0].message
        content = message.content or ""
        reasoning = getattr(message, "reasoning_content", None) or getattr(
            message, "reasoning", None
        )
        usage = completion.usage
        return LLMResult(
            text=content,
            model=completion.model or self.settings.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            reasoning=str(reasoning) if isinstance(reasoning, str) else "",
        )

    def _create_with_retry(self, kwargs: dict):
        """发起请求；上游以 400 拒绝且我们附加了 extra_body 时，去掉附加
        参数立即重试一次。

        部分兼容端点不接受我们默认附加的私有参数（如为默认关闭思考而加的
        enable_thinking=false；GLM Coding 端点对这类模型返回 1210「不支持
        关闭思考」）。审核/分类等调用不携带面板开关，无法靠覆盖规避——
        模型按自身默认行为输出即可，省略参数是无害回退。
        """
        try:
            return self.client.chat.completions.create(**kwargs)
        except APIStatusError as error:
            if getattr(error, "status_code", None) == 400 and kwargs.get("extra_body"):
                logger.warning(
                    "上游拒绝 extra_body（%s）；去掉附加参数重试一次",
                    str(getattr(error, "body", ""))[:200],
                )
                retry_kwargs = {
                    key: value for key, value in kwargs.items() if key != "extra_body"
                }
                return self.client.chat.completions.create(**retry_kwargs)
            raise

    def _create_stream(self, kwargs: dict):
        return self._create_with_retry(kwargs)

    def generate_stream_with_options(
        self,
        messages,
        *,
        max_output_tokens=None,
        timeout=None,
        sampling=None,
        extra_body_overrides=None,
        on_delta=None,
        escalate_on_truncate=False,
    ) -> LLMResult:
        effective_timeout = (
            min(self.settings.timeout, timeout)
            if timeout is not None
            else self.settings.timeout
        )
        ceiling = self.settings.max_output_tokens
        token_limit = max_output_tokens or ceiling
        # 「思考无法关闭」的模型（GLM Coding 等）会把大半输出预算花在推理
        # 上：正文为空，或判定 JSON 被截成半截。辅助调用允许在截断时按所选
        # 模型的输出上限重试一次；回合级预算与停止通道仍然生效。
        escalation_left = 1 if escalate_on_truncate and token_limit < ceiling else 0
        # 「思考吃满预算且正文为空」的紧急放宽：不依赖 escalate 标志（生成
        # 调用本来就在模型上限运行，常规升级无处可升），上限取模型上下文的
        # 安全经验值；仅一次。
        empty_burst_left = 1
        while True:
            char_cap = min(self._STREAM_CHAR_CAP, max(1, int(token_limit)) * 8)
            # 流式读超时收紧为「字节间最大静默」：上游滴漏式响应（隔很久才
            # 来一个字节）会绕过整体超时与逐事件截止检查，无限期挂住回合。
            # 思考模型的正常分片间隔是秒级，300 秒静默即为异常。
            import httpx

            stream_timeout = httpx.Timeout(
                connect=15.0,
                read=min(300.0, effective_timeout),
                write=60.0,
                pool=15.0,
            )
            kwargs = {
                "model": self.settings.model,
                "messages": messages,
                self.settings.token_parameter: token_limit,
                "timeout": stream_timeout,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if isinstance(sampling, dict):
                for key in (
                    "temperature",
                    "top_p",
                    "frequency_penalty",
                    "presence_penalty",
                ):
                    if key in sampling:
                        kwargs[key] = sampling[key]
            extra = dict(self.settings.extra_body)
            if isinstance(extra_body_overrides, dict):
                extra.update(extra_body_overrides)
            if extra:
                kwargs["extra_body"] = extra

            if generation_abort_requested():
                raise GenerationAborted("用户停止了本轮生成")
            deadline = time.monotonic() + effective_timeout
            stream = self._create_stream(kwargs)
            parts = []
            reasoning_parts = []
            finish_reason = None
            usage = None
            model = self.settings.model
            accumulated_chars = 0
            try:
                for event in stream:
                    if generation_abort_requested():
                        raise GenerationAborted("用户停止了本轮生成")
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
                        reasoning_delta = getattr(
                            delta, "reasoning_content", None
                        ) or getattr(delta, "reasoning", None)
                        if isinstance(reasoning_delta, str):
                            reasoning_parts.append(reasoning_delta)
                    if on_delta is not None and (parts or reasoning_parts):
                        # 传累计全文而非单段增量：消费者可检测长度回退（重生成
                        # 时文本重新开始）并整体重置自己的展示状态。
                        try:
                            on_delta("".join(parts), "".join(reasoning_parts))
                        except Exception:  # noqa: BLE001 - 展示层异常不影响生成
                            pass
                if time.monotonic() > deadline:
                    raise TimeoutError("流式响应超过本轮截止时间")
                text = "".join(parts)
                if finish_reason == "length" and escalation_left:
                    escalation_left -= 1
                    token_limit = ceiling
                    logger.info(
                        "辅助调用输出被截断（finish=length）；放宽至 %s 重试一次",
                        token_limit,
                    )
                    continue
                if (
                    finish_reason == "length"
                    and not text.strip()
                    and empty_burst_left
                    and token_limit < self._EMPTY_BURST_CEILING
                ):
                    # 思考锁死的模型可能把整个输出预算花在推理上（正文 0 字），
                    # 此时按模型上限的重试救不回来——上限本来就已用满。放宽到
                    # 紧急上限（模型上下文允许时）再试一次：思考照旧烧，但正文
                    # 有空间写完。上游不支持更大值时会以 4xx 拒绝，回落原报错。
                    empty_burst_left -= 1
                    token_limit = min(
                        max(token_limit * 4, self._EMPTY_BURST_CEILING // 2),
                        self._EMPTY_BURST_CEILING,
                    )
                    logger.warning(
                        "输出预算被思考耗尽（finish=length, text=0）；放宽至 %s 重试一次",
                        token_limit,
                    )
                    continue
                if finish_reason != "stop":
                    # 触顶截断（finish_reason=length）且正文有内容：思考模型的
                    # reasoning 也计入 completion 预算，正文被截断是常见情况，
                    # 照常送审交付比整轮报错更有用；其余中止原因（内容过滤/
                    # 工具调用/缺失标记）不可交付。
                    if finish_reason != "length" or not text.strip():
                        logger.warning(
                            "流式响应异常结束 finish_reason=%s text_chars=%s reasoning_chars=%s",
                            finish_reason,
                            len(text),
                            len("".join(reasoning_parts)),
                        )
                        raise RuntimeError("流式响应不完整：缺少 stop 结束标记或被截断/过滤")
                return LLMResult(
                    text=text,
                    model=model,
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                    reasoning="".join(reasoning_parts),
                )
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()


def make_backend(settings: Settings) -> ChatBackend:
    if settings.mode == "mock":
        return MockChatBackend()
    return OpenAICompatibleBackend(settings)

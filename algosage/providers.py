"""Server-owned provider/model allowlist. Browser selections never carry credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .runtime_paths import runtime_path

# BYOK（自带密钥）自定义 Provider 默认只允许接入小参数量模型：小模型的输出更
# 容易被本系统的规则与 LLM Judge 审核层可靠约束。SAGE_CUSTOM_MODEL_MAX_B 可
# 调整上限；设为 0 或负数表示暂不限制（审核层照常工作，但大模型的输出更长、
# 更难约束，属于部署方的权衡）。
DEFAULT_CUSTOM_MODEL_MAX_B = 27.0

_UNLIMITED = float("inf")


def custom_model_max_b() -> float:
    """参数量上限（B）；环境变量设为 0 或负数表示暂不限制。"""
    try:
        value = float(
            os.environ.get("SAGE_CUSTOM_MODEL_MAX_B", "") or DEFAULT_CUSTOM_MODEL_MAX_B
        )
    except ValueError:
        return DEFAULT_CUSTOM_MODEL_MAX_B
    return value if value > 0 else _UNLIMITED


_MOE_SIZE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])"
)
_PLAIN_SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])")


def parse_model_params_b(model_id: str) -> float | None:
    """Best-effort parameter-size read from the model id ("8B" -> 8.0).

    MoE ids like "8x7B" count the total (8*7=56). Returns None when no size
    marker is present; callers must treat that as "unverifiable".
    """
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    moe = _MOE_SIZE.search(model_id)
    if moe:
        return float(moe.group(1)) * float(moe.group(2))
    plain = _PLAIN_SIZE.search(model_id)
    if plain:
        return float(plain.group(1))
    return None


def validate_public_base_url(url: str) -> str:
    """Same URL policy as the server allowlist: HTTPS only, no credentials."""
    candidate = (url or "").strip()
    parsed = urlparse(candidate)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (loopback and parsed.scheme == "http"))
    ):
        raise ValueError(
            "Base URL 必须使用 HTTPS（本机 loopback 除外），且不能携带账号或查询参数"
        )
    return candidate


def build_custom_settings(
    base: Settings,
    base_url: str,
    api_key: str,
    model: str,
    capabilities: dict | None = None,
) -> Settings:
    """A per-user BYOK provider profile; same trusted Settings the pipeline uses."""
    url = validate_public_base_url(base_url)
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
        raise ValueError("API Key 不能为空且长度需在 4096 字符以内")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("模型名不能为空且长度需在 200 字符以内")
    params_b = parse_model_params_b(model)
    if custom_model_max_b() != _UNLIMITED:
        if params_b is None:
            raise ValueError(
                "无法从模型名识别参数量（需要形如 8B / 14B 的标注），为稳妥起见拒绝添加"
            )
        if params_b > custom_model_max_b():
            raise ValueError(
                f"模型参数量 {params_b:g}B 超过自定义上限 {custom_model_max_b():g}B"
            )
    return replace(
        base,
        mode="api",
        api_key=api_key,
        base_url=url,
        model=model,
        capabilities=capabilities,
    )


_NON_CHAT_MARKERS = ("embedding", "reranker", "asr", "tts", "audio", "voice", "speech")
_VISION_MARKERS = ("vl", "omni", "vision", "internvl")
_THINKING_PATTERNS = (
    "qwen3",
    "deepseek-r1",
    "glm-z1",
    "qwq",
    "thinking",
    "reasoner",
)


def short_model_name(model_id: str) -> str:
    """展示用短名：去掉组织前缀（如 Qwen/Qwen3-8B → Qwen3-8B）。"""
    return model_id.rsplit("/", 1)[-1].strip()


def _marker_hit(model_id: str, markers: tuple[str, ...]) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in markers)


def is_chat_model(model_id: str) -> bool:
    """按命名规则排除非对话模型（Embedding/Reranker/语音类不可作为聊天后端）。"""
    return not _marker_hit(model_id, _NON_CHAT_MARKERS)


def infer_model_capabilities(model_id: str) -> dict[str, bool]:
    """Heuristic capability flags from the model id.

    上游 /models 接口不返回能力字段，只能依据命名规则推断：
    - 思考：Qwen3 系列、DeepSeek-R1、GLM-Z1、QwQ、名字含 thinking/reasoner；
    - 多模态：名字含 VL / Omni / Vision；
    - 联网搜索：OpenAI 兼容推理服务（如 SiliconFlow）对文本对话模型统一
      支持 enable_search 参数，非对话模型一律不支持。
    """
    chat = is_chat_model(model_id)
    if not chat:
        return {
            "is_chat": False,
            "thinking": False,
            "multimodal": False,
            "web_search": False,
        }
    lowered = model_id.lower()
    multimodal = _marker_hit(model_id, _VISION_MARKERS)
    thinking = (
        (
            "qwen3" in lowered
            and not multimodal
            and "thinking" not in lowered
        )
        or "deepseek-r1" in lowered
        or "glm-z1" in lowered
        or "qwq" in lowered
        or "thinking" in lowered
        or "reasoner" in lowered
    )
    return {
        "is_chat": True,
        "thinking": thinking,
        "multimodal": multimodal,
        "web_search": True,
    }


def capability_overrides(settings: Settings, deep_think: bool, web_search: bool) -> dict:
    """能力感知透传：开关打开且模型支持时才向 extra_body 传对应参数。

    优先使用 BYOK 注册时真实探测并持久化的能力（settings.capabilities）；
    无探测数据（预置模型）回退命名推断。探测不明确（None）、缺失或为
    False 一律视为不支持，不透传。
    """
    probed = getattr(settings, "capabilities", None)
    caps = (
        probed
        if isinstance(probed, dict) and probed
        else infer_model_capabilities(settings.model)
    )
    overrides: dict = {}
    if deep_think and caps.get("thinking"):
        overrides["enable_thinking"] = True
    if web_search and caps.get("web_search"):
        overrides["enable_search"] = True
    return overrides


def custom_selection_id(label: str, base_url: str, model: str) -> str:
    digest = hashlib.sha1(
        f"{label}|{base_url}|{model}".encode("utf-8", "ignore")
    ).hexdigest()[:10]
    return f"custom:{digest}"


class ProviderRegistry:
    """Server-configured providers. 无兜底：操作员未配置 providers.json 时
    用户目录为空，由前端禁用发送并引导用户添加自己的提供商。"""

    def __init__(self, base: Settings, entries: list[dict] | None = None):
        self.base = base
        self._settings: dict[str, Settings] = {}
        self._options: list[dict] = []
        if entries is None:
            path = runtime_path("SAGE_PROVIDERS_FILE", ".algosage/providers.json")
            if path and Path(path).is_file():
                parsed = json.loads(Path(path).read_text(encoding="utf-8"))
                if not isinstance(parsed, dict) or set(parsed) != {"providers"}:
                    raise ValueError("Provider 文件必须仅包含 providers 列表")
                entries = parsed["providers"]
            elif os.environ.get("SAGE_PROVIDERS_FILE", "").strip():
                raise ValueError("SAGE_PROVIDERS_FILE 不存在")
        if entries is not None:
            if not isinstance(entries, list) or len(entries) > 32:
                raise ValueError("providers 必须是至多 32 项的列表")
            for entry in entries:
                self._add(entry)
        self.default = self._options[0]["selection"] if self._options else ""

    def _add(self, entry, default=False):
        allowed = {
            "id",
            "label",
            "mode",
            "models",
            "base_url",
            "api_key_env",
            "extra_body",
            "token_parameter",
        }
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError(
                "Provider 含未知字段；密钥仅支持 api_key_env 引用，禁止修改安全策略"
            )
        provider_id = entry.get("id", "")
        if not isinstance(provider_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,48}", provider_id
        ):
            raise ValueError("Provider id 非法")
        if not default and provider_id == "default":
            raise ValueError("default 为保留 Provider id")
        models = entry.get("models")
        if (
            not isinstance(models, list)
            or not 1 <= len(models) <= 64
            or any(
                not isinstance(m, str) or not m.strip() or len(m) > 200 for m in models
            )
        ):
            raise ValueError("Provider models 必须是非空模型名称列表")
        mode = entry.get("mode", "api")
        if mode not in {"api", "mock"}:
            raise ValueError("Provider mode 必须为 api 或 mock")
        label = entry.get("label", provider_id)
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("Provider label 非法")
        key_env = entry.get("api_key_env", "LLM_API_KEY")
        if not isinstance(key_env, str) or not re.fullmatch(
            r"[A-Z_][A-Z0-9_]*", key_env
        ):
            raise ValueError("api_key_env 必须为环境变量名")
        key = (
            self.base.api_key
            if default or key_env == "LLM_API_KEY"
            else os.environ.get(key_env, "")
        )
        url = entry.get("base_url", self.base.base_url)
        parsed = urlparse(url)
        if mode == "api" and (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise ValueError(
                "Provider base_url 必须为 HTTPS（loopback HTTP 除外），禁止 URL 内凭据"
            )
        extra = entry.get("extra_body", self.base.extra_body if default else {})
        if not isinstance(extra, dict) or set(extra) & {
            "model",
            "messages",
            "stream",
            "stream_options",
            "api_key",
            "max_tokens",
            "max_completion_tokens",
        }:
            raise ValueError("Provider extra_body 非法或覆盖请求控制字段")
        token_parameter = entry.get("token_parameter", self.base.token_parameter)
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Provider token_parameter 非法")
        for model in models:
            selection = provider_id + "::" + model
            if selection in self._settings:
                raise ValueError("Provider/model 重复")
            settings = replace(
                self.base,
                mode=mode,
                api_key=key,
                base_url=url,
                model=model,
                token_parameter=token_parameter,
                extra_body=dict(extra),
            )
            self._settings[selection] = settings
            self._options.append(
                {
                    "id": provider_id,
                    "label": label,
                    "model": model,
                    "selection": selection,
                    "mode": mode,
                    "available": mode == "mock" or bool(key),
                }
            )

    def options(self) -> list[dict]:
        return [dict(o) for o in self._options]

    def resolve(self, selection: str) -> Settings:
        if not isinstance(selection, str) or selection not in self._settings:
            raise ValueError("未知 Provider / 模型，请使用服务端配置的选项")
        option = next(o for o in self._options if o["selection"] == selection)
        if not option["available"]:
            raise ValueError("该 Provider 未配置服务端凭据")
        return replace(
            self._settings[selection],
            extra_body=dict(self._settings[selection].extra_body),
        )

    def info(self, selection: str) -> dict:
        self.resolve(selection)
        return next(dict(o) for o in self._options if o["selection"] == selection)

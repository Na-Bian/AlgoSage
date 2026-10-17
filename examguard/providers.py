"""Server-owned provider/model allowlist. Browser selections never carry credentials."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings


class ProviderRegistry:
    def __init__(self, base: Settings, entries: list[dict] | None = None):
        self.base = base
        self._settings: dict[str, Settings] = {}
        self._options: list[dict] = []
        default = {
            "id": "default",
            "label": "默认 Provider",
            "mode": base.mode,
            "models": [base.model],
        }
        self._add(default, default=True)
        if entries is None:
            path = os.environ.get(
                "EG_PROVIDERS_FILE", ".examguard/providers.json"
            ).strip()
            if path and Path(path).is_file():
                parsed = json.loads(Path(path).read_text(encoding="utf-8"))
                if not isinstance(parsed, dict) or set(parsed) != {"providers"}:
                    raise ValueError("Provider 文件必须仅包含 providers 列表")
                entries = parsed["providers"]
            elif os.environ.get("EG_PROVIDERS_FILE", "").strip():
                raise ValueError("EG_PROVIDERS_FILE 不存在")
        if entries is not None:
            if not isinstance(entries, list) or len(entries) > 32:
                raise ValueError("providers 必须是至多 32 项的列表")
            for entry in entries:
                self._add(entry)
        self.default = self._options[0]["selection"]

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

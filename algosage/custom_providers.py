"""Per-principal BYOK provider registrations.

Users may bring their own OpenAI-compatible endpoint (Base URL + API Key).
Entries live only in this process's memory keyed by the trusted principal id:

- API keys are never persisted (not in SQLite, not in chat history) and never
  returned to any client after registration;
- every entry is still executed through the full audited AlgoSage pipeline
  (classification → graded help policy → output guard), only the model backend
  changes;
- model discovery reuses the same HTTPS-only URL policy and only accepts
  models whose id carries a parameter-size marker within the custom limit.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .config import Settings
from .providers import (
    build_custom_settings,
    custom_model_max_b,
    custom_selection_id,
    parse_model_params_b,
    validate_public_base_url,
)

MAX_CUSTOM_PROVIDERS_PER_USER = 8
DISCOVERY_MODEL_LIMIT = 200
CUSTOM_PROVIDER_LABEL = "自定义 API"


class CustomProviderStore:
    def __init__(self, base: Settings, fetch_models=None):
        self._base = base
        # principal_id -> selection -> entry dict (LRU capped per principal)
        self._entries: dict[str, OrderedDict[str, dict]] = {}
        self._lock = threading.RLock()
        # Injectable for tests: fetch_models(base_url, api_key) -> [model_id]
        self._fetch_models = fetch_models or self._default_fetch_models

    # ------------------------------------------------------------------
    # Registration / listing
    # ------------------------------------------------------------------
    def register(
        self,
        principal_id: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        label: str = "",
    ) -> dict:
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("缺少可信身份，不能注册自定义模型")
        name = (label or "").strip() or CUSTOM_PROVIDER_LABEL
        if len(name) > 40:
            name = name[:40]
        settings = build_custom_settings(self._base, base_url, api_key, model)
        selection = custom_selection_id(name, settings.base_url, model)
        entry = {
            "id": "custom",
            "label": name,
            "model": model,
            "selection": selection,
            "mode": "api",
            "available": True,
            "params_b": parse_model_params_b(model),
            "base_url": settings.base_url,
            # The key deliberately stays inside this process only.
            "api_key": api_key,
            "settings": settings,
        }
        with self._lock:
            bucket = self._entries.setdefault(principal_id, OrderedDict())
            bucket.pop(selection, None)
            bucket[selection] = entry
            while len(bucket) > MAX_CUSTOM_PROVIDERS_PER_USER:
                bucket.popitem(last=False)
        return self._public_entry(entry)

    def remove(self, principal_id: str, selection: str) -> bool:
        with self._lock:
            bucket = self._entries.get(principal_id)
            if not bucket or selection not in bucket:
                return False
            del bucket[selection]
            return True

    def list_for(self, principal_id: str) -> list[dict]:
        with self._lock:
            bucket = self._entries.get(principal_id)
            entries = list(bucket.values()) if bucket else []
        return [self._public_entry(entry) for entry in entries]

    def resolve(self, principal_id: str, selection: str) -> Settings:
        with self._lock:
            bucket = self._entries.get(principal_id)
            entry = bucket.get(selection) if bucket else None
            if entry is None:
                raise ValueError("未知自定义模型，或所属会话已失效，请重新添加")
            bucket.move_to_end(selection)
            settings: Settings = entry["settings"]
            return settings

    def info(self, principal_id: str, selection: str) -> dict:
        with self._lock:
            bucket = self._entries.get(principal_id)
            entry = bucket.get(selection) if bucket else None
            if entry is None:
                raise ValueError("未知自定义模型")
            return {"label": entry["label"], "model": entry["model"], "id": "custom"}

    @staticmethod
    def _public_entry(entry: dict) -> dict:
        """Whitelisted view for the browser; never includes the API key."""
        return {
            "id": "custom",
            "label": entry["label"],
            "model": entry["model"],
            "selection": entry["selection"],
            "mode": "api",
            "available": True,
            "params_b": entry["params_b"],
        }

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------
    def discover(self, base_url: str, api_key: str) -> list[dict]:
        url = validate_public_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("API Key 不能为空")
        limit_b = custom_model_max_b()
        models = []
        for model_id in self._fetch_models(url, api_key)[:DISCOVERY_MODEL_LIMIT]:
            params_b = parse_model_params_b(model_id)
            models.append(
                {
                    "id": model_id,
                    "params_b": params_b,
                    "allowed": params_b is not None and params_b <= limit_b,
                }
            )
        models.sort(
            key=lambda item: (
                not item["allowed"],
                item["params_b"] is None,
                str(item["id"]).lower(),
            )
        )
        return models

    @staticmethod
    def _default_fetch_models(base_url: str, api_key: str) -> list[str]:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15, max_retries=0)
        # OpenAI-compatible /models listing; some vendors paginate but a single
        # page is enough for interactive picking.
        response = client.models.list()
        return [item.id for item in response.data]

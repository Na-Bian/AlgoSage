"""BYOK custom provider tests: size parsing, store isolation, service wiring."""

import os
import unittest
from pathlib import Path
from unittest import mock

from algosage.config import Settings
from algosage.custom_providers import CustomProviderStore
from algosage.providers import (
    capability_overrides,
    build_custom_settings,
    custom_selection_id,
    infer_model_capabilities,
    parse_model_params_b,
)
from algosage.service import TutorService


def mock_settings(**overrides):
    base = {"mode": "mock", "api_key": "", "state_path": ""}
    base.update(overrides)
    return Settings(**base)


class ParseModelSizeTests(unittest.TestCase):
    def test_plain_sizes(self):
        self.assertEqual(parse_model_params_b("Qwen/Qwen3-8B"), 8.0)
        self.assertEqual(parse_model_params_b("Qwen2.5-14B-Instruct"), 14.0)
        self.assertEqual(parse_model_params_b("glm-4-9b-chat"), 9.0)
        self.assertEqual(parse_model_params_b("mini-0.6b"), 0.6)
        self.assertEqual(parse_model_params_b("26B"), 26.0)

    def test_moe_uses_total(self):
        self.assertEqual(parse_model_params_b("Mixtral-8x7B-Instruct"), 56.0)
        self.assertEqual(parse_model_params_b("Qwen3-Coder-30B-A3B"), 30.0)

    def test_unknown_or_oversized(self):
        self.assertIsNone(parse_model_params_b("gpt-4o-mini"))
        self.assertIsNone(parse_model_params_b("embedding-3"))
        self.assertEqual(parse_model_params_b("Llama-3.3-70B"), 70.0)
        self.assertIsNone(parse_model_params_b(""))
        self.assertIsNone(parse_model_params_b(None))


class BuildCustomSettingsTests(unittest.TestCase):
    def test_builds_api_profile(self):
        settings = build_custom_settings(
            mock_settings(max_output_tokens=512),
            "https://api.example.com/v1",
            "sk-test",
            "Qwen/Qwen3-8B",
        )
        self.assertEqual(settings.mode, "api")
        self.assertEqual(settings.api_key, "sk-test")
        self.assertEqual(settings.base_url, "https://api.example.com/v1")
        self.assertEqual(settings.model, "Qwen/Qwen3-8B")
        self.assertIsNone(settings.capabilities)

    def test_carries_probed_capabilities(self):
        caps = {
            "thinking": True,
            "multimodal": False,
            "web_search": True,
            "is_chat": True,
        }
        settings = build_custom_settings(
            mock_settings(),
            "https://api.example.com/v1",
            "sk-test",
            "Qwen/Qwen3-8B",
            capabilities=caps,
        )
        self.assertEqual(settings.capabilities, caps)

    def test_capability_overrides_prefer_probed_over_naming(self):
        # 命名推断 Qwen3-8B 支持思考；真实探测说不支持 → 不透传。
        settings = build_custom_settings(
            mock_settings(),
            "https://api.example.com/v1",
            "sk",
            "Qwen3-8B",
            capabilities={"thinking": False, "web_search": False, "is_chat": True},
        )
        self.assertEqual(capability_overrides(settings, True, True), {})
        # 命名推断 Qwen2.5 不支持思考；真实探测说支持 → 透传。
        settings = build_custom_settings(
            mock_settings(),
            "https://api.example.com/v1",
            "sk",
            "Qwen2.5-7B-Instruct",
            capabilities={"thinking": True, "web_search": True, "is_chat": True},
        )
        self.assertEqual(
            capability_overrides(settings, True, True),
            {"enable_thinking": True, "enable_search": True},
        )
        # 探测不明确（None）视为不支持。
        settings = build_custom_settings(
            mock_settings(),
            "https://api.example.com/v1",
            "sk",
            "Qwen3-8B",
            capabilities={"thinking": None, "web_search": None, "is_chat": True},
        )
        self.assertEqual(capability_overrides(settings, True, True), {})
        # 无探测数据（预置模型）回退命名推断。
        settings = build_custom_settings(
            mock_settings(), "https://api.example.com/v1", "sk", "Qwen3-8B"
        )
        self.assertEqual(
            capability_overrides(settings, True, False), {"enable_thinking": True}
        )
        # 开关关闭时不透传。
        self.assertEqual(capability_overrides(settings, False, True), {"enable_search": True})

    def test_rejects_insecure_or_credentialed_urls(self):
        for url in (
            "http://api.example.com/v1",
            "https://user:pw@api.example.com/v1",
            "https://api.example.com/v1?x=1",
            "ftp://api.example.com",
            "",
        ):
            with self.assertRaises(ValueError, msg=url):
                build_custom_settings(mock_settings(), url, "sk", "Qwen3-8B")

    def test_rejects_oversized_or_unverifiable_models(self):
        for model in ("Llama-3.3-70B", "gpt-4o-mini", ""):
            with self.assertRaises(ValueError, msg=model):
                build_custom_settings(
                    mock_settings(), "https://api.example.com/v1", "sk", model
                )

    def test_limit_is_configurable(self):
        with mock.patch.dict(os.environ, {"SAGE_CUSTOM_MODEL_MAX_B": "32"}):
            build_custom_settings(
                mock_settings(), "https://api.example.com/v1", "sk", "Qwen2.5-32B"
            )


class CustomProviderStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = CustomProviderStore(mock_settings(max_output_tokens=512))

    def register_sample(self, principal="u1", model="Qwen/Qwen3-8B"):
        return self.store.register(
            principal,
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model=model,
            label="我的接口",
        )

    def test_register_and_public_view_hides_key(self):
        entry = self.register_sample()
        self.assertTrue(entry["selection"].startswith("custom:"))
        self.assertNotIn("api_key", entry)
        self.assertNotIn("settings", entry)
        self.assertEqual(entry["model"], "Qwen/Qwen3-8B")

    def test_resolve_returns_settings_and_isolates_principals(self):
        entry = self.register_sample()
        settings = self.store.resolve("u1", entry["selection"])
        self.assertEqual(settings.model, "Qwen/Qwen3-8B")
        with self.assertRaises(ValueError):
            self.store.resolve("u2", entry["selection"])

    def test_remove_and_no_registration_cap(self):
        # 不再限制用户可添加的自定义提供商数量。
        for index in range(10):
            self.store.register(
                "u1",
                base_url=f"https://api{index}.example.com/v1",
                api_key="sk",
                model="Qwen3-8B",
            )
        self.assertEqual(len(self.store.list_for("u1")), 10)
        entry = self.register_sample()
        self.assertTrue(self.store.remove("u1", entry["selection"]))
        self.assertFalse(self.store.remove("u1", entry["selection"]))

    def test_discover_filters_by_size_with_injected_fetcher(self):
        store = CustomProviderStore(
            mock_settings(),
            fetch_models=lambda url, key: [
                "Qwen/Qwen3-8B",
                "Qwen2.5-72B",
                "gpt-4o-mini",
                "Qwen2.5-7B",
            ],
        )
        models = store.discover("https://api.example.com/v1", "sk")
        by_id = {item["id"]: item for item in models}
        self.assertTrue(by_id["Qwen/Qwen3-8B"]["allowed"])
        self.assertTrue(by_id["Qwen2.5-7B"]["allowed"])
        self.assertFalse(by_id["Qwen2.5-72B"]["allowed"])
        self.assertFalse(by_id["gpt-4o-mini"]["allowed"])
        # 可用模型排在最前，方便前端展示。
        self.assertEqual(models[0]["id"], "Qwen/Qwen3-8B")
        # 列表默认不带能力标签，仅 is_chat 做非对话排除；真实能力点选后懒探测。
        for item in models:
            self.assertEqual(set(item["capabilities"]), {"is_chat"})

    def test_register_keeps_probed_capabilities(self):
        # 真实探测结果（含 None 的未知项）优先于命名推断，不得被覆盖。
        capabilities = {
            "thinking": True,
            "multimodal": None,
            "web_search": True,
            "is_chat": True,
        }
        entry = self.store.register(
            "u1",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="Qwen2.5-7B-Instruct",
            capabilities=capabilities,
        )
        self.assertEqual(entry["capabilities"], capabilities)
        # 未传入探测结果时回退命名推断。
        fallback = self.store.register(
            "u2",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="Qwen2.5-7B-Instruct",
        )
        self.assertEqual(
            fallback["capabilities"], infer_model_capabilities("Qwen2.5-7B-Instruct")
        )

    def test_discover_validates_url_and_key(self):
        store = CustomProviderStore(mock_settings(), fetch_models=lambda u, k: [])
        with self.assertRaises(ValueError):
            store.discover("http://api.example.com", "sk")
        with self.assertRaises(ValueError):
            store.discover("https://api.example.com", "  ")


class TutorServiceCustomProviderTests(unittest.TestCase):
    def setUp(self):
        self.service = TutorService(
            mock_settings(classifier_mode="regex", output_guard="basic")
        )

    def test_custom_selection_flows_through_service(self):
        entry = self.service.register_custom_provider(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="Qwen/Qwen3-8B",
            label="我的接口",
        )
        context = self.service.new_context("s1", principal_id="student-a")
        self.service.select_provider(context, entry["selection"])
        self.assertEqual(context.provider_selection, entry["selection"])
        info = self.service.provider_info(context)
        self.assertEqual(info["model"], "Qwen/Qwen3-8B")
        # 采样参数按自定义 profile 收敛，密钥绝不进入持久化参数。
        self.assertNotIn("api_key", str(context.model_params))
        tutor = self.service._selected_tutor(context)
        self.assertEqual(tutor.backend.settings.model, "Qwen/Qwen3-8B")
        # 不同身份不能复用他人的自定义模型。
        other = self.service.new_context("s2", principal_id="student-b")
        with self.assertRaises(ValueError):
            self.service.select_provider(other, entry["selection"])

    def test_no_server_fallback_provider(self):
        # 无 providers.json 时不存在服务端兜底：default 为空、不可选择。
        context = self.service.new_context("s3", principal_id="student-a")
        self.assertEqual(self.service.providers.default, "")
        self.assertEqual(context.provider_selection, "")
        with self.assertRaises(ValueError):
            self.service.select_provider(context, "")

    def test_remove_custom_provider(self):
        entry = self.service.register_custom_provider(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk",
            model="Qwen3-8B",
        )
        self.assertTrue(
            self.service.remove_custom_provider("student-a", entry["selection"])
        )
        context = self.service.new_context("s4", principal_id="student-a")
        with self.assertRaises(ValueError):
            self.service.select_provider(context, entry["selection"])

    def test_selection_ids_stable(self):
        self.assertEqual(
            custom_selection_id("a", "https://x", "m"),
            custom_selection_id("a", "https://x", "m"),
        )
        self.assertNotEqual(
            custom_selection_id("a", "https://x", "m"),
            custom_selection_id("b", "https://x", "m"),
        )


if __name__ == "__main__":
    unittest.main()


class CustomProviderPersistenceTests(unittest.TestCase):
    """Fernet-sealed .env persistence for BYOK entries."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = str(Path(self.tmp.name) / "env")
        Path(self.env_path).write_text(
            "LLM_API_KEY=existing\n# comment stays\n", encoding="utf-8"
        )
        self.base = Settings(mode="mock")

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        return CustomProviderStore(self.base, env_file=self.env_path)

    def test_register_seals_key_and_preserves_env(self):
        store = self._store()
        entry = store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-plaintext-secret",
            model="Qwen3-8B",
            label="我的 端点",
        )
        text = Path(self.env_path).read_text(encoding="utf-8")
        self.assertNotIn("sk-plaintext-secret", text)
        self.assertIn("gAAAA", text)  # Fernet token
        self.assertIn("LLM_API_KEY=existing", text)
        self.assertIn("# comment stays", text)
        self.assertIn(entry["selection"], store.list_for("student-a")[0]["selection"])

    def test_entries_survive_store_restart(self):
        store = self._store()
        entry = store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-keep",
            model="Qwen3-8B",
            label="我的 端点",
        )
        revived = self._store()
        entries = revived.list_for("student-a")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["selection"], entry["selection"])
        settings = revived.resolve("student-a", entry["selection"])
        self.assertEqual(settings.api_key, "sk-keep")

    def test_probed_capabilities_survive_restart(self):
        # 注册时携带真实探测结果，重启后必须还原到 settings.capabilities，
        # 不得回退命名推断（Qwen3-8B 命名会说支持思考，探测结果为不支持）。
        store = self._store()
        entry = store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-caps",
            model="Qwen3-8B",
            label="探测条目",
            capabilities={
                "thinking": False,
                "multimodal": None,
                "web_search": True,
                "is_chat": True,
            },
        )
        revived = self._store()
        settings = revived.resolve("student-a", entry["selection"])
        # 持久化标签只保留 True 项；缺失键等价于不支持，绝无命名推断的 True。
        self.assertEqual(
            settings.capabilities, {"is_chat": True, "web_search": True}
        )
        self.assertIsNone(settings.capabilities.get("thinking"))

    def test_remove_updates_env_and_restart(self):
        store = self._store()
        entry = store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-x",
            model="Qwen3-8B",
        )
        self.assertTrue(store.remove("student-a", entry["selection"]))
        revived = self._store()
        self.assertEqual(revived.list_for("student-a"), [])
        text = Path(self.env_path).read_text(encoding="utf-8")
        self.assertNotIn("api.example.com", text)

    def test_corrupt_or_foreign_secret_skips_entries(self):
        store = self._store()
        store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk-x",
            model="Qwen3-8B",
        )
        text = Path(self.env_path).read_text(encoding="utf-8")
        # 篡改托管区块的加密密钥 → 重启后条目被跳过而非崩溃。
        text = text.replace(
            "SAGE_CUSTOM_PROVIDER_SECRET=", "SAGE_CUSTOM_PROVIDER_SECRET=X", 1
        )
        Path(self.env_path).write_text(text, encoding="utf-8")
        revived = self._store()
        self.assertEqual(revived.list_for("student-a"), [])

    def test_newline_in_fields_rejected(self):
        store = self._store()
        with self.assertRaises(ValueError):
            store.register(
                "student-a",
                base_url="https://api.example.com/v1\nEVIL=1",
                api_key="sk",
                model="Qwen3-8B",
            )

    def test_memory_only_default_writes_nothing(self):
        store = CustomProviderStore(self.base)
        store.register(
            "student-a",
            base_url="https://api.example.com/v1",
            api_key="sk",
            model="Qwen3-8B",
        )
        text = Path(self.env_path).read_text(encoding="utf-8")
        self.assertNotIn("SAGE_CUSTOM_PROVIDER", text)

"""官方 API Key 对话、离线兜底与模型配置持久化测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.pet_settings import (
    API_PROVIDER_AGENT_PLATFORM,
    API_PROVIDER_GEMINI_DEVELOPER,
    DEFAULT_AGENT_PLATFORM_API_VERSION,
    DEFAULT_AGENT_PLATFORM_LOCATION,
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    format_model_display_name,
    parse_agent_platform_json_config,
    resolve_effective_thinking_level,
    validate_agent_platform_project,
)
from services.gemini_chat_service import ApiConnectionConfig, GeminiChatService
from services.settings_service import SettingsService


class TestChatAndSettings(unittest.TestCase):
    def test_unsupported_thinking_level_falls_back_to_model_default(self) -> None:
        self.assertEqual(resolve_effective_thinking_level("gemini-3.1-flash-lite", "xhigh"), "auto")
        self.assertEqual(resolve_effective_thinking_level("gemini-4-hydrogen", "xhigh"), "xhigh")

    def test_api_key_chat_uses_official_client_and_keeps_context(self) -> None:
        client = MagicMock()
        client.__enter__.return_value = client
        client.interactions.create.side_effect = [
            SimpleNamespace(id="demo-turn-1", output_text="第一轮回复"),
            SimpleNamespace(id="demo-turn-2", output_text="第二轮回复"),
        ]
        settings = PetSettings(gemini_api_key="demo-api-key", gemini_model=DEFAULT_GEMINI_MODEL)
        chat = GeminiChatService()
        with patch("google.genai.Client", return_value=client) as factory:
            self.assertEqual(chat.ask("分析一下这段算法", settings).text, "第一轮回复")
            self.assertEqual(chat.ask("继续解释", settings).text, "第二轮回复")
        self.assertEqual(factory.call_count, 2)
        for call in factory.call_args_list:
            self.assertFalse(call.kwargs["vertexai"])
            self.assertEqual(call.kwargs["api_key"], "demo-api-key")
            self.assertEqual(
                call.kwargs["http_options"]["base_url"],
                "https://generativelanguage.googleapis.com",
            )
        first, second = client.interactions.create.call_args_list
        self.assertNotIn("previous_interaction_id", first.kwargs)
        self.assertEqual(second.kwargs["previous_interaction_id"], "demo-turn-1")

    def test_missing_key_stays_offline_without_creating_client(self) -> None:
        chat = GeminiChatService()
        settings = PetSettings()
        with patch("google.genai.Client") as factory:
            self.assertIsNone(chat.resolve_chat_provider(settings))
            self.assertTrue(chat.ask("你好呀", settings).text)
            with self.assertRaisesRegex(ValueError, "API Key"):
                chat.fetch_models_for_chat(settings)
            factory.assert_not_called()

    def test_offline_mode_preserves_key_and_blocks_official_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = SettingsService(tmp)
            settings = PetSettings(gemini_api_key="demo-api-key", chat_offline_mode=True)
            service.save(settings)
            loaded = service.load()
            self.assertTrue(loaded.chat_offline_mode)
            self.assertEqual(loaded.gemini_api_key, "demo-api-key")

            chat = GeminiChatService()
            with patch("google.genai.Client") as factory:
                self.assertIsNone(chat.resolve_chat_provider(loaded))
                self.assertTrue(chat.ask("陪我聊聊吧", loaded).text)
                self.assertEqual(chat.fetch_models_for_chat(loaded), [])
                factory.assert_not_called()

    def test_default_model_and_explicit_choice_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = SettingsService(tmp)
            settings = service.load()
            self.assertEqual(settings.gemini_model, DEFAULT_GEMINI_MODEL)
            self.assertEqual(
                format_model_display_name(None), format_model_display_name(DEFAULT_GEMINI_MODEL)
            )
            settings.gemini_model = "gemini-2.5-flash"
            service.save(settings)
            self.assertEqual(service.load().gemini_model, "gemini-2.5-flash")
            settings.gemini_model = ""
            service.save(settings)
            self.assertEqual(service.load().gemini_model, DEFAULT_GEMINI_MODEL)

    def test_api_key_error_does_not_follow_a_changed_key(self) -> None:
        from google.genai.errors import ClientError

        settings = PetSettings(gemini_api_key="demo-invalid-key")
        chat = GeminiChatService()
        with patch.object(
            chat,
            "_call_gemini_api",
            side_effect=ClientError(403, {"error": {"message": "Permission denied"}}),
        ):
            self.assertIn("API Key", chat.ask("解释这个算法", settings).text)
        self.assertIn("API Key", chat.get_api_error(settings))
        settings.gemini_api_key = "demo-replacement-key"
        self.assertEqual(chat.get_api_error(settings), "")

    def test_api_key_fetch_models_filters_chat_models(self) -> None:
        model_names = [
            "models/gemini-2.5-pro",
            "models/gemini-embedding-001",
            f"models/{DEFAULT_GEMINI_MODEL}",
            "models/imagen-3.0-generate-002",
            "models/gemini-3-flash-preview",
        ]
        actions = [
            ["generateContent"],
            ["embedContent"],
            ["generateContent", "countTokens"],
            ["predict"],
            ["createInteraction"],
        ]
        fake_models = []
        for model_name, supported in zip(model_names, actions):
            obj = MagicMock(supported_actions=supported)
            obj.name = model_name
            fake_models.append(obj)

        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.models.list.return_value = fake_models

        GeminiChatService._api_models_cache.clear()
        with patch("google.genai.Client", return_value=fake_client):
            fetched = GeminiChatService.fetch_api_key_models("AIza-test-models")
        self.assertEqual(fetched, [DEFAULT_GEMINI_MODEL, "gemini-2.5-pro", "gemini-3-flash-preview"])

    def test_agent_platform_express_mode_uses_global_endpoint_and_keeps_history(self) -> None:
        client = MagicMock()
        client.__enter__.return_value = client
        client.models.generate_content.side_effect = [
            SimpleNamespace(text="Express 第一轮"),
            SimpleNamespace(text="Express 第二轮"),
        ]
        settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-express-key",
            agent_platform_project="",
            agent_platform_location="us-central1",
            agent_platform_api_version="v1beta1",
            gemini_model=DEFAULT_GEMINI_MODEL,
        )
        chat = GeminiChatService()
        with patch("google.genai.Client", return_value=client) as factory:
            self.assertEqual(chat.ask("分析这段代码", settings).text, "Express 第一轮")
            self.assertEqual(chat.ask("继续优化一下", settings).text, "Express 第二轮")

        self.assertEqual(factory.call_count, 2)
        for call in factory.call_args_list:
            self.assertTrue(call.kwargs["enterprise"])
            self.assertEqual(call.kwargs["api_key"], "demo-agent-express-key")
            self.assertNotIn("project", call.kwargs)
            self.assertNotIn("location", call.kwargs)
            self.assertEqual(
                call.kwargs["http_options"]["base_url"],
                "https://aiplatform.googleapis.com",
            )
            self.assertEqual(call.kwargs["http_options"]["api_version"], "v1beta1")

        client.interactions.create.assert_not_called()
        second_contents = client.models.generate_content.call_args_list[1].kwargs["contents"]
        self.assertEqual([item["role"] for item in second_contents], ["user", "model", "user"])
        self.assertEqual(second_contents[1]["parts"][0]["text"], "Express 第一轮")

    def test_agent_platform_project_location_endpoints_and_interactions_fallback(self) -> None:
        from google.genai._gaos.google_genai import compat_errors

        self.assertEqual(
            GeminiChatService.resolve_agent_platform_endpoint("demo-project", "global", "v1"),
            ("https://aiplatform.googleapis.com", "demo-project", "global", "v1"),
        )
        self.assertEqual(
            GeminiChatService.resolve_agent_platform_endpoint("demo-project", "us", "v1beta1"),
            ("https://aiplatform.us.rep.googleapis.com", "demo-project", "us", "v1beta1"),
        )
        self.assertEqual(
            GeminiChatService.resolve_agent_platform_endpoint("demo-project", "us-central1", "v1"),
            ("https://us-central1-aiplatform.googleapis.com", "demo-project", "us-central1", "v1"),
        )

        req = compat_errors.httpx.Request("POST", "https://us-central1-aiplatform.googleapis.com/v1/interactions")
        resp = compat_errors.httpx.Response(404, request=req, text="Not Found")
        not_found_err = compat_errors.NotFoundError(
            "not found", response=resp, body={"error": {"message": "not found"}}
        )

        client = MagicMock()
        client.__enter__.return_value = client
        client.interactions.create.side_effect = not_found_err
        client.models.generate_content.side_effect = [
            SimpleNamespace(text="项目模式回退回复"),
            SimpleNamespace(text="项目模式二轮回复"),
        ]
        settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-project-key",
            agent_platform_project="demo-project-123",
            agent_platform_location="us-central1",
            agent_platform_api_version="v1",
            gemini_model=DEFAULT_GEMINI_MODEL,
        )
        chat = GeminiChatService()
        with patch("google.genai.Client", return_value=client) as factory:
            self.assertEqual(chat.ask("解释分布式锁", settings).text, "项目模式回退回复")
            self.assertEqual(chat.ask("再举个例子", settings).text, "项目模式二轮回复")

        self.assertEqual(client.interactions.create.call_count, 1)
        self.assertEqual(client.models.generate_content.call_count, 2)
        first_call = factory.call_args_list[0]
        self.assertTrue(first_call.kwargs["enterprise"])
        self.assertEqual(first_call.kwargs["project"], "demo-project-123")
        self.assertEqual(first_call.kwargs["location"], "us-central1")
        self.assertEqual(
            first_call.kwargs["http_options"]["base_url"],
            "https://us-central1-aiplatform.googleapis.com",
        )
        self.assertEqual(first_call.kwargs["http_options"]["api_version"], "v1")

    def test_dual_keys_persist_switch_and_isolate_api_errors(self) -> None:
        from google.genai.errors import ClientError

        with tempfile.TemporaryDirectory() as tmp:
            service = SettingsService(tmp)
            settings = PetSettings(
                api_provider=API_PROVIDER_AGENT_PLATFORM,
                gemini_api_key="demo-dev-key",
                agent_platform_api_key="demo-agent-key",
                agent_platform_project="demo-gcp-proj",
                agent_platform_location="asia-northeast1",
                agent_platform_api_version="v1",
            )
            service.save(settings)
            loaded = service.load()
            self.assertEqual(loaded.api_provider, API_PROVIDER_AGENT_PLATFORM)
            self.assertEqual(loaded.gemini_api_key, "demo-dev-key")
            self.assertEqual(loaded.agent_platform_api_key, "demo-agent-key")
            self.assertEqual(loaded.agent_platform_project, "demo-gcp-proj")
            self.assertEqual(loaded.agent_platform_location, "asia-northeast1")
            self.assertEqual(loaded.agent_platform_api_version, "v1")

            chat = GeminiChatService()
            self.assertEqual(chat.resolve_chat_provider(loaded), API_PROVIDER_AGENT_PLATFORM)
            with patch.object(
                chat,
                "_call_gemini_api",
                side_effect=ClientError(403, {"error": {"message": "Permission denied"}}),
            ):
                self.assertIn("API Key", chat.ask("测试权限", loaded).text)
            self.assertIn("API Key", chat.get_api_error(loaded))

            # 切换回 Gemini API 渠道后，旧渠道错误不污染新渠道，且双 Key 各自保留。
            loaded.api_provider = API_PROVIDER_GEMINI_DEVELOPER
            self.assertEqual(chat.resolve_chat_provider(loaded), API_PROVIDER_GEMINI_DEVELOPER)
            self.assertEqual(chat.get_api_error(loaded), "")

            # 切换回 Agent Platform 但清空 Key 时保持离线。
            loaded.api_provider = API_PROVIDER_AGENT_PLATFORM
            loaded.agent_platform_api_key = ""
            self.assertIsNone(chat.resolve_chat_provider(loaded))

    def test_agent_platform_model_parsing_and_list_unsupported_error(self) -> None:
        from google.genai.errors import ClientError

        self.assertEqual(
            format_model_display_name("projects/demo-proj/locations/global/publishers/google/models/gemini-3.1-flash-lite"),
            format_model_display_name(DEFAULT_GEMINI_MODEL),
        )
        fake_models = [
            MagicMock(name="publishers/google/models/gemini-4-hydrogen", supported_actions=["generateContent"]),
            MagicMock(name="projects/p/locations/us-central1/publishers/google/models/gemini-embedding-001", supported_actions=["embedContent"]),
            MagicMock(name=f"projects/p/locations/us-central1/publishers/google/models/{DEFAULT_GEMINI_MODEL}", supported_actions=["generateContent"]),
        ]
        # MagicMock(name=...) 设置的是 mock 自身名，需显式赋 .name 属性。
        fake_models[0].name = "publishers/google/models/gemini-4-hydrogen"
        fake_models[1].name = "projects/p/locations/us-central1/publishers/google/models/gemini-embedding-001"
        fake_models[2].name = f"projects/p/locations/us-central1/publishers/google/models/{DEFAULT_GEMINI_MODEL}"

        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.models.list.return_value = fake_models

        GeminiChatService._api_models_cache.clear()
        cfg = ApiConnectionConfig(
            provider=API_PROVIDER_AGENT_PLATFORM,
            api_key="demo-agent-key",
            project="demo-proj",
            location=DEFAULT_AGENT_PLATFORM_LOCATION,
            api_version=DEFAULT_AGENT_PLATFORM_API_VERSION,
        )
        with patch("google.genai.Client", return_value=fake_client):
            fetched = GeminiChatService.fetch_models_for_config(cfg)
        self.assertEqual(fetched, [DEFAULT_GEMINI_MODEL, "gemini-4-hydrogen"])

        # models.list 报 "API keys are not supported" 时自动回退到 count_tokens 探测。
        probe_client = MagicMock()
        probe_client.__enter__.return_value = probe_client
        probe_client.__exit__.return_value = False
        probe_client.models.list.side_effect = ClientError(
            401, {"error": {"message": "API keys are not supported by this API."}}
        )
        supported_set = {DEFAULT_GEMINI_MODEL, "gemini-4-hydrogen", "gemini-2.5-flash"}

        def _fake_count_tokens(*, model: str, contents: str):
            del contents
            if model in supported_set:
                return SimpleNamespace(total_tokens=2)
            raise ClientError(404, {"error": {"message": "Model not found"}})

        probe_client.models.count_tokens.side_effect = _fake_count_tokens
        settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-express-key",
        )
        chat = GeminiChatService()
        GeminiChatService._api_models_cache.clear()
        with patch("google.genai.Client", return_value=probe_client):
            probed = chat.fetch_models_for_chat(settings)
        self.assertEqual(probed, [DEFAULT_GEMINI_MODEL, "gemini-4-hydrogen", "gemini-2.5-flash"])
        self.assertEqual(GeminiChatService.get_cached_models_for_settings(settings), probed)
        self.assertEqual(chat.get_api_error(settings), "")

        # 当填了 Project ID 且 list 与 count_tokens 均报不支持 API Key 时，提示清空 Project ID。
        reject_client = MagicMock()
        reject_client.__enter__.return_value = reject_client
        reject_client.__exit__.return_value = False
        reject_client.models.list.side_effect = ClientError(
            401, {"error": {"message": "API keys are not supported by this API."}}
        )
        reject_client.models.count_tokens.side_effect = ClientError(
            401, {"error": {"message": "API keys are not supported by this API."}}
        )
        proj_settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-express-key",
            agent_platform_project="demo-proj-123",
        )
        with patch("google.genai.Client", return_value=reject_client):
            with self.assertRaisesRegex(RuntimeError, "清空 Project ID"):
                chat.fetch_models_for_chat(proj_settings)

    def test_agent_platform_validation_rejects_malformed_project_or_location(self) -> None:
        self.assertIn("OAuth Client ID", validate_agent_platform_project("123456-demo.apps.googleusercontent.com") or "")
        self.assertIn("服务账号", validate_agent_platform_project("demo@demo-proj.iam.gserviceaccount.com") or "")
        self.assertIn("API Key", validate_agent_platform_project("AQ.DemoKeyPlaceholder") or "")
        self.assertIn("API Key", validate_agent_platform_project("AIzaDemoKeyPlaceholder") or "")
        with self.assertRaises(ValueError):
            GeminiChatService.resolve_agent_platform_endpoint("bad project/..", "global", "v1beta1")
        with self.assertRaises(ValueError):
            GeminiChatService.resolve_agent_platform_endpoint(
                "demo-proj", "evil.example.com#", "v1beta1"
            )

    def test_retry_options_and_ssl_eof_probe_circuit_breaker(self) -> None:
        from google.genai.errors import ClientError

        with patch("google.genai.Client") as factory:
            GeminiChatService._create_api_client(
                ApiConnectionConfig(
                    provider=API_PROVIDER_AGENT_PLATFORM,
                    api_key="demo-agent-key",
                ),
                15000,
            )
        http_opts = factory.call_args.kwargs["http_options"]
        self.assertEqual(http_opts["retry_options"]["attempts"], 2)

        ssl_client = MagicMock()
        ssl_client.__enter__.return_value = ssl_client
        ssl_client.__exit__.return_value = False
        ssl_client.models.list.side_effect = ClientError(
            401, {"error": {"message": "API keys are not supported by this API."}}
        )
        ssl_client.models.count_tokens.side_effect = OSError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)"
        )
        settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-express-key",
        )
        chat = GeminiChatService()
        with patch("google.genai.Client", return_value=ssl_client):
            with self.assertRaisesRegex(RuntimeError, "SSL EOF"):
                chat.fetch_models_for_chat(settings)
        # 连续两次网络/TLS 异常即熔断，避免逐一重试全部候选模型。
        self.assertEqual(ssl_client.models.count_tokens.call_count, 2)
        self.assertEqual(chat.get_api_error(settings), "")

    def test_401_403_error_detail_scrubbing_and_project_403_early_exit(self) -> None:
        from google.genai.errors import ClientError

        iam_err = ClientError(
            403,
            {
                "error": {
                    "message": "Permission 'aiplatform.endpoints.predict' denied on resource '//aiplatform.googleapis.com/projects/demo'."
                }
            },
        )
        iam_msg = GeminiChatService._format_api_error(iam_err, provider=API_PROVIDER_AGENT_PLATFORM)
        self.assertIn("API Key", iam_msg)
        self.assertIn("Agent Platform User", iam_msg)
        self.assertIn("roles/aiplatform.user", iam_msg)
        self.assertIn("aiplatform.endpoints.predict", iam_msg)

        billing_err = ClientError(
            403,
            {"error": {"message": "This API method requires billing to be enabled on project."}},
        )
        billing_msg = GeminiChatService._format_api_error(
            billing_err, provider=API_PROVIDER_AGENT_PLATFORM
        )
        self.assertIn("结算（Billing）", billing_msg)

        token_err = ClientError(
            401,
            {"error": {"message": "API key not valid for token AQ.DemoFakeToken123456"}},
        )
        token_msg = GeminiChatService._format_api_error(token_err)
        self.assertIn("HTTP 401", token_msg)
        self.assertIn("[REDACTED]", token_msg)
        self.assertNotIn("AQ.DemoFakeToken123456", token_msg)

        # 项目级 403 权限不足应在首个模型探测时即退出，避免重复打完所有候选模型。
        iam_client = MagicMock()
        iam_client.__enter__.return_value = iam_client
        iam_client.__exit__.return_value = False
        iam_client.models.list.side_effect = iam_err
        iam_client.models.count_tokens.side_effect = iam_err
        settings = PetSettings(
            api_provider=API_PROVIDER_AGENT_PLATFORM,
            agent_platform_api_key="demo-agent-key",
            agent_platform_project="demo-proj",
        )
        chat = GeminiChatService()
        with patch("google.genai.Client", return_value=iam_client):
            with self.assertRaisesRegex(RuntimeError, "roles/aiplatform.user"):
                chat.fetch_models_for_chat(settings)
        self.assertEqual(iam_client.models.count_tokens.call_count, 1)

    def test_parse_agent_platform_json_config(self) -> None:
        installed_json = json.dumps(
            {
                "installed": {
                    "client_id": "demo-client.apps.googleusercontent.com",
                    "project_id": "demo-gcp-proj",
                    "client_secret": "demo-secret-placeholder",
                }
            }
        )
        self.assertEqual(parse_agent_platform_json_config(installed_json), ("demo-gcp-proj", None))

        web_json = json.dumps(
            {"web": {"project_id": "demo-web-proj"}, "location": "us-central1"}
        )
        self.assertEqual(parse_agent_platform_json_config(web_json), ("demo-web-proj", "us-central1"))

        with self.assertRaisesRegex(ValueError, "project_id"):
            parse_agent_platform_json_config(json.dumps({"installed": {"client_id": "demo"}}))
        with self.assertRaises(ValueError):
            parse_agent_platform_json_config(json.dumps({"installed": {"project_id": "bad proj/.."}}))


if __name__ == "__main__":
    unittest.main()

"""官方 API Key 对话、离线兜底与模型配置持久化测试。"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    format_model_display_name,
    resolve_effective_thinking_level,
)
from services.gemini_chat_service import GeminiChatService
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


if __name__ == "__main__":
    unittest.main()

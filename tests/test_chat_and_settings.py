"""官方 API Key 模型获取与设置窗口测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton

from models.pet_settings import (
    DEFAULT_GEMINI_MODEL,
    PetSettings,
    format_model_display_name,
    resolve_effective_thinking_level,
)
from services.app_follow_service import AppFollowService
from services.gemini_chat_service import GeminiChatService
from services.settings_service import SettingsService
from views.settings_window import SettingsWindow


def _isolated_env(home_dir: str, appdata_dir: str) -> dict[str, str]:
    return {
        "USERPROFILE": home_dir,
        "HOME": home_dir,
        "APPDATA": appdata_dir,
        "LOCALAPPDATA": appdata_dir,
        "PATH": os.environ.get("PATH", ""),
    }


class TestChatAndSettings(unittest.TestCase):
    @staticmethod
    def _ensure_app() -> QApplication:
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        return app

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
            self.assertEqual(call.kwargs["http_options"]["base_url"], "https://generativelanguage.googleapis.com")
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

    def test_chat_menu_offline_mode_preserves_key_and_persists(self) -> None:
        from services.sound_service import SoundService
        from services.sprite_manager import SpriteManager
        from services.system_monitor_service import SystemMonitorService
        from views.pet_window import PetWindow

        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            settings = PetSettings(gemini_api_key="demo-api-key", auto_walk=False)
            service = SettingsService(tmp)
            service.save(settings)
            chat = GeminiChatService()
            pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), chat)
            try:
                pet.open_chat_dialog()
                menu = QMenu(pet)
                pet._populate_connection_menu(menu)
                offline = next(a for a in menu.actions() if a.data() == ("chat_mode", "offline"))
                online = next(a for a in menu.actions() if a.data() == ("chat_mode", "api_key"))
                self.assertTrue(online.isChecked())
                with (
                    patch.object(chat, "_call_gemini_api", return_value="在线回复") as request,
                    patch.object(chat, "fetch_api_key_models") as fetch,
                ):
                    offline.trigger()
                    self.assertTrue(service.load().chat_offline_mode)
                    self.assertEqual(settings.gemini_api_key, "demo-api-key")
                    self.assertEqual(pet._chat_channel_badge.text(), "离线模式")
                    self.assertTrue(chat.ask("陪我聊聊吧", settings).text)
                    self.assertEqual(chat.fetch_models_for_chat(settings), [])
                    pet._refresh_chat_models_async(explicit=True)
                    self.assertIsNone(pet._chat_models_discovery_worker)
                    request.assert_not_called()
                    fetch.assert_not_called()

                    rebuilt = pet._build_chat_model_menu()
                    refresh = next(a for a in rebuilt.actions() if "获取 / 刷新官方模型" in a.text())
                    self.assertFalse(refresh.isEnabled())
                    online.trigger()
                    self.assertFalse(service.load().chat_offline_mode)
                    self.assertEqual(pet._chat_channel_badge.text(), "在线 · API Key")
                    self.assertEqual(chat.ask("继续解释算法", settings).text, "在线回复")
                    request.assert_called_once()
                    self.assertEqual(request.call_args.args[2], DEFAULT_GEMINI_MODEL)

                    # 清空 Key 后实际按离线运行，菜单勾选需与状态徽章一致
                    settings.gemini_api_key = ""
                    pet._sync_chat_controls()
                    self.assertEqual(pet._chat_channel_badge.text(), "离线模式")
                    keyless_menu = QMenu(pet)
                    pet._populate_connection_menu(keyless_menu)
                    keyless_modes = {
                        action.data()[1]: action
                        for action in keyless_menu.actions()
                        if action.data() and action.data()[0] == "chat_mode"
                    }
                    self.assertTrue(keyless_modes["offline"].isChecked())
                    self.assertFalse(keyless_modes["api_key"].isChecked())
                    keyless_modes["api_key"].trigger()
                    self.assertIn("Gemini API Key", pet._bubble_text_lbl.text())
                    self.assertTrue(keyless_modes["offline"].isChecked())
                    self.assertFalse(keyless_modes["api_key"].isChecked())
            finally:
                pet.close()
                pet.deleteLater()
                app.processEvents()

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
        with patch.object(chat, "_call_gemini_api", side_effect=ClientError(403, {"error": {"message": "Permission denied"}})):
            self.assertIn("API Key", chat.ask("解释这个算法", settings).text)
        self.assertIn("API Key", chat.get_api_error(settings))
        settings.gemini_api_key = "demo-replacement-key"
        self.assertEqual(chat.get_api_error(settings), "")

    def test_api_key_fetch_models_settings_manual_confirm_and_save_feedback(self) -> None:
        app = self._ensure_app()
        fake_models = [
            MagicMock(name="models/gemini-2.5-pro", supported_actions=["generateContent"]),
            MagicMock(name="models/gemini-embedding-001", supported_actions=["embedContent"]),
            MagicMock(name=f"models/{DEFAULT_GEMINI_MODEL}", supported_actions=["generateContent", "countTokens"]),
            MagicMock(name="models/imagen-3.0-generate-002", supported_actions=["predict"]),
            MagicMock(name="models/gemini-3-flash-preview", supported_actions=["createInteraction"]),
        ]
        # MagicMock(name=...) sets the mock repr name, so assign `.name` explicitly as model field
        for obj, model_name in zip(
            fake_models,
            [
                "models/gemini-2.5-pro",
                "models/gemini-embedding-001",
                f"models/{DEFAULT_GEMINI_MODEL}",
                "models/imagen-3.0-generate-002",
                "models/gemini-3-flash-preview",
            ],
        ):
            obj.name = model_name

        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.models.list.return_value = fake_models

        GeminiChatService._api_models_cache.clear()
        with patch("google.genai.Client", return_value=fake_client):
            fetched = GeminiChatService.fetch_api_key_models("AIza-test-models")
        self.assertEqual(fetched, [DEFAULT_GEMINI_MODEL, "gemini-2.5-pro", "gemini-3-flash-preview"])

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            with patch.dict(os.environ, _isolated_env(tmp_home, tmp_appdata)):
                svc = SettingsService(tmp)
                settings = PetSettings(gemini_api_key="AIza-test-models")
                svc.save(settings)
                follow_svc = AppFollowService(svc)
                applied_calls = 0

                def on_applied() -> None:
                    nonlocal applied_calls
                    applied_calls += 1

                with patch("views.settings_window.FollowedAppProbe.sample_antigravity_activity") as mock_ag, patch(
                    "views.settings_window.FollowedAppProbe.sample_gemini_activity"
                ) as mock_gem:
                    mock_ag.return_value = MagicMock(is_running=False)
                    mock_gem.return_value = MagicMock(is_running=False)
                    GeminiChatService._api_models_cache.clear()
                    win = SettingsWindow(
                        settings=settings,
                        settings_service=svc,
                        app_follow_service=follow_svc,
                        on_settings_applied=on_applied,
                    )
                    try:
                        # 获取前不写死大模型清单，仅显示当前配置的默认模型
                        self.assertEqual(win._model_combo.currentText(), DEFAULT_GEMINI_MODEL)
                        self.assertEqual(
                            [win._model_combo.itemText(i) for i in range(win._model_combo.count())],
                            [DEFAULT_GEMINI_MODEL],
                        )
                        # 验证 对话模型 / Gemini API Key / 思考强度 三行均采用统一水平标签布局
                        self.assertIsNotNone(win._model_combo.parentWidget())
                        # 模拟获取官方 API Key 模型结果并填充下拉列表
                        with patch("google.genai.Client", return_value=fake_client):
                            fetched = GeminiChatService.fetch_api_key_models(settings.gemini_api_key)
                        win._on_models_refreshed(fetched, "")
                        combo_items = [win._model_combo.itemText(i) for i in range(win._model_combo.count())]
                        self.assertIn("gemini-2.5-pro", combo_items)
                        self.assertIn("gemini-3-flash-preview", combo_items)
                        self.assertIn("已获取 3 个官方可用模型", win._api_models_hint_lbl.text())

                        # 修改草稿后、未点确认前：既不触发 on_applied 也不写盘，可点击还原修改恢复
                        win._model_combo.setEditText("gemini-2.5-pro")
                        app.processEvents()
                        self.assertEqual(settings.gemini_model, DEFAULT_GEMINI_MODEL)
                        self.assertEqual(applied_calls, 0)
                        self.assertTrue(win.has_unsaved_changes())
                        win._discard_btn.click()
                        app.processEvents()
                        self.assertFalse(win.has_unsaved_changes())
                        self.assertEqual(win._model_combo.currentText(), DEFAULT_GEMINI_MODEL)

                        # 再次修改并点击确认修改 -> 写盘并显示保存成功
                        win._model_combo.setEditText("gemini-2.5-pro")
                        win._confirm_btn.click()
                        app.processEvents()
                        self.assertEqual(settings.gemini_model, "gemini-2.5-pro")
                        self.assertEqual(svc.load().gemini_model, "gemini-2.5-pro")
                        self.assertEqual(applied_calls, 1)
                        self.assertIn("保存成功", win._save_feedback_lbl.text())

                        # 当磁盘保存失败时，应回滚运行态改动并明确提示保存失败
                        win._model_combo.setEditText("gemini-3-flash-preview")
                        with patch.object(svc, "save", side_effect=PermissionError("只读目录")):
                            win._confirm_btn.click()
                            app.processEvents()
                        self.assertEqual(settings.gemini_model, "gemini-2.5-pro")
                        self.assertTrue(win.has_unsaved_changes())
                        self.assertIn("保存失败", win._save_feedback_lbl.text())
                        self.assertIn("只读目录", win._save_feedback_lbl.text())
                    finally:
                        win.close()
                        win.deleteLater()
                        app.processEvents()

    def test_settings_all_page_search_filters_and_navigates(self) -> None:
        """全部设置汇总页应支持关键词过滤，并可点击跳转到对应分类。"""
        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            settings = PetSettings()
            win = SettingsWindow(
                settings=settings,
                settings_service=svc,
                app_follow_service=AppFollowService(svc),
                on_settings_applied=lambda: None,
            )
            try:
                win.show()
                app.processEvents()
                self.assertEqual(len(win._summary_rows), 25)
                self.assertTrue(all(not row.isHidden() for row, _text, _getter in win._summary_rows))

                win._search_edit.setText("气泡")
                app.processEvents()
                matched = [row for row, _text, _getter in win._summary_rows if not row.isHidden()]
                self.assertTrue(matched)
                self.assertLess(len(matched), 25)
                self.assertFalse(win._summary_empty.isVisible())

                win._search_edit.setText("不存在的设置项")
                app.processEvents()
                self.assertTrue(all(row.isHidden() for row, _text, _getter in win._summary_rows))
                self.assertTrue(win._summary_empty.isVisible())

                win._search_edit.clear()
                app.processEvents()
                self.assertTrue(all(not row.isHidden() for row, _text, _getter in win._summary_rows))
                self.assertFalse(win._summary_empty.isVisible())

                # 汇总行点击后跳到对应分类并勾选左侧导航
                win._summary_rows[0][0].activated.emit(SettingsWindow._PAGE_CHAT)
                app.processEvents()
                self.assertEqual(win._stack.currentIndex(), SettingsWindow._PAGE_CHAT)
                self.assertTrue(win._nav_buttons[SettingsWindow._PAGE_CHAT].isChecked())
            finally:
                win.close()
                win.deleteLater()
                app.processEvents()

    def test_settings_summary_quick_actions_trigger_callbacks(self) -> None:
        """全部设置末尾的快捷操作应直接打开角色设定、角色档案与更新窗口。"""
        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            calls = {"editor": 0, "profile": 0, "update": 0}
            win = SettingsWindow(
                settings=PetSettings(),
                settings_service=svc,
                app_follow_service=AppFollowService(svc),
                on_settings_applied=lambda: None,
                on_open_update_center=lambda: calls.__setitem__("update", calls["update"] + 1),
                on_open_character_editor=lambda: calls.__setitem__("editor", calls["editor"] + 1),
                on_open_profile=lambda: calls.__setitem__("profile", calls["profile"] + 1),
            )
            try:
                win.show()
                app.processEvents()
                self.assertEqual(len(win._summary_rows), 25)
                for (row, _text, _getter), key in zip(
                    win._summary_rows[-3:], ("editor", "profile", "update")
                ):
                    QTest.mouseClick(row, Qt.MouseButton.LeftButton)
                    app.processEvents()
                    self.assertEqual(calls[key], 1, key)
            finally:
                win.close()
                win.deleteLater()
                app.processEvents()

    def test_character_page_actions_trigger_callbacks(self) -> None:
        """角色设定分类页应提供自定义角色设定与角色档案两个入口。"""
        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            calls = {"editor": 0, "profile": 0}
            win = SettingsWindow(
                settings=PetSettings(),
                settings_service=svc,
                app_follow_service=AppFollowService(svc),
                on_settings_applied=lambda: None,
                on_open_character_editor=lambda: calls.__setitem__("editor", calls["editor"] + 1),
                on_open_profile=lambda: calls.__setitem__("profile", calls["profile"] + 1),
            )
            try:
                win.show()
                app.processEvents()
                win._select_page(SettingsWindow._PAGE_CHARACTER)
                app.processEvents()
                page = win._stack.widget(SettingsWindow._PAGE_CHARACTER)
                buttons = [btn for btn in page.findChildren(QPushButton) if btn.text() == "打开"]
                self.assertEqual(len(buttons), 2)
                for button in buttons:
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    app.processEvents()
                self.assertEqual(calls, {"editor": 1, "profile": 1})
            finally:
                win.close()
                win.deleteLater()
                app.processEvents()

    def test_bubble_setting_belongs_to_behavior_page(self) -> None:
        """弹出默认气泡属于行为互动，不应留在通用设置里。"""
        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            win = SettingsWindow(
                settings=PetSettings(),
                settings_service=svc,
                app_follow_service=AppFollowService(svc),
                on_settings_applied=lambda: None,
            )
            try:
                win.show()
                app.processEvents()
                behavior_page = win._stack.widget(SettingsWindow._PAGE_BEHAVIOR)
                general_page = win._stack.widget(SettingsWindow._PAGE_GENERAL)
                self.assertTrue(behavior_page.isAncestorOf(win._chk_bubble))
                self.assertFalse(general_page.isAncestorOf(win._chk_bubble))

                bubble_row = next(
                    row
                    for row, _text, _getter in win._summary_rows
                    if any(lbl.text() == "弹出默认气泡" for lbl in row.findChildren(QLabel))
                )
                self.assertEqual(bubble_row._target, SettingsWindow._PAGE_BEHAVIOR)
            finally:
                win.close()
                win.deleteLater()
                app.processEvents()

    def test_appearance_sliders_preview_then_save(self) -> None:
        """缩放/透明度滑杆拖动时应实时预览，确认后才写入配置。"""
        app = self._ensure_app()
        with tempfile.TemporaryDirectory() as tmp:
            svc = SettingsService(tmp)
            settings = PetSettings(scale=1.0, opacity=1.0)
            svc.save(settings)
            previews: list[tuple[float | None, float | None]] = []
            win = SettingsWindow(
                settings=settings,
                settings_service=svc,
                app_follow_service=AppFollowService(svc),
                on_settings_applied=lambda: None,
                on_preview_appearance=lambda scale, opacity: previews.append((scale, opacity)),
            )
            try:
                win.show()
                app.processEvents()
                self.assertEqual(previews, [])

                win._scale_slider.setValue(130)
                app.processEvents()
                self.assertAlmostEqual(previews[-1][0], 1.3)
                self.assertAlmostEqual(settings.scale, 1.0)  # 预览不写运行配置
                self.assertEqual(svc.load().scale, 1.0)  # 也不写盘

                win._opacity_slider.setValue(70)
                app.processEvents()
                self.assertAlmostEqual(previews[-1][1], 0.7)
                self.assertAlmostEqual(settings.opacity, 1.0)

                # 还原修改 -> 撤销预览并回到已保存值
                win._discard_btn.click()
                app.processEvents()
                self.assertEqual(previews[-1], (None, None))
                self.assertAlmostEqual(win._scale_slider.value(), 100)
                self.assertAlmostEqual(win._opacity_slider.value(), 100)
                self.assertFalse(win.has_unsaved_changes())

                # 再次调整并确认 -> 真正落盘
                win._scale_slider.setValue(120)
                win._opacity_slider.setValue(80)
                app.processEvents()
                win._confirm_btn.click()
                app.processEvents()
                self.assertAlmostEqual(settings.scale, 1.2)
                self.assertAlmostEqual(settings.opacity, 0.8)
                self.assertAlmostEqual(svc.load().scale, 1.2)
                self.assertAlmostEqual(svc.load().opacity, 0.8)
            finally:
                win.close()
                win.deleteLater()
                app.processEvents()

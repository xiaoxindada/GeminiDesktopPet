"""设置窗口、菜单开机启动项与额度弹窗定位/自动关闭行为测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from app import PetApplicationCoordinator
from models.pet_settings import DockSide, PetSettings
from models.pet_state import PetState
from services.antigravity_quota_service import (
    AntigravityQuotaService,
    QuotaBucketInfo,
    QuotaGroupInfo,
    QuotaSnapshot,
)
from services.settings_service import SettingsService
from views.quota_window import QuotaWindow


def _sample_snapshot() -> QuotaSnapshot:
    return QuotaSnapshot(
        success=True,
        error_message=None,
        source_detail="Antigravity app · pid 1234 · http://127.0.0.1:56789",
        account_email=None,
        account_plan="Google AI Pro",
        overview_description=None,
        fetched_at=datetime.now(timezone.utc),
        groups=[
            QuotaGroupInfo(
                "Gemini Models",
                "Gemini pool",
                [
                    QuotaBucketInfo("w", "Weekly Limit Remaining", "weekly", 0.5, datetime.now(timezone.utc)),
                    QuotaBucketInfo("5h", "Five Hour Limit Remaining", "5h", 0.8, datetime.now(timezone.utc)),
                ],
            )
        ],
    )


class TestSettingsAndAutostartMenu(unittest.TestCase):
    @staticmethod
    def _ensure_app() -> QApplication:
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        return app

    @staticmethod
    def _find_action(menu, substring: str):
        for act in menu.actions():
            if not act.isSeparator() and substring in act.text():
                return act
        raise AssertionError(f"Action containing {substring!r} not found in top-level menu")

    def test_settings_open_reopen_restore_and_first_level_autostart(self) -> None:
        app = self._ensure_app()
        with (
            tempfile.TemporaryDirectory() as tmp_startup,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            startup_dir = Path(tmp_startup)
            fake_settings = PetSettings()
            with (
                patch.dict(os.environ, {"USERPROFILE": tmp_home, "APPDATA": tmp_appdata}),
                patch("services.win32_ipc.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.SettingsService.load", return_value=fake_settings),
                patch("services.settings_service.SettingsService.save", return_value=None),
                patch("services.app_follow_service.AppFollowService.sync_and_apply", return_value=None),
                patch.object(PetApplicationCoordinator, "_start_silent_update_check", return_value=None),
            ):
                coord = PetApplicationCoordinator(app, [], MagicMock())
                try:
                    coord._pet.show()
                    app.processEvents()

                    # 桌面菜单：聊天/固定/打盹/气泡/同步/缩放/日志/角色/更新/设置/退出
                    pet_menu_texts = [
                        act.text() for act in coord._pet._context_menu.actions() if not act.isSeparator()
                    ]
                    for expected in (
                        "聊天模式",
                        "固定（自由漫步）",
                        "打盹模式",
                        "自动弹出对话气泡",
                        "实时同步反重力对话",
                        "形象切换",
                        "缩放大小",
                        "记录运行日志",
                        "自定义角色设定",
                        "角色档案",
                        "检查更新",
                        "设置",
                        "退出",
                        "查看反重力额度",
                        "回到屏幕右下角",
                    ):
                        self.assertTrue(any(expected in text for text in pet_menu_texts), expected)
                    for removed in ("开机自动启动", "反重力悬浮", "贴靠屏幕边缘", "动作表现", "鼠标穿透"):
                        self.assertFalse(any(removed in text for text in pet_menu_texts), removed)

                    # 托盘菜单 = 桌面菜单同一批 QAction + 唤醒类专用操作
                    tray_texts = [
                        act.text() for act in coord._tray._menu.actions() if not act.isSeparator()
                    ]
                    for dedicated in ("呼唤 Gemini 娘",):
                        self.assertTrue(any(dedicated in text for text in tray_texts), dedicated)
                    for shared in (
                        "聊天模式",
                        "固定（自由漫步）",
                        "打盹模式",
                        "自动弹出对话气泡",
                        "实时同步反重力对话",
                        "记录运行日志",
                        "查看反重力额度",
                        "回到屏幕右下角",
                        "自定义角色设定",
                        "角色档案",
                        "检查更新",
                        "设置",
                        "退出",
                    ):
                        pet_act = self._find_action(coord._pet._context_menu, shared)
                        tray_act = self._find_action(coord._tray._menu, shared)
                        self.assertIs(pet_act, tray_act, shared)

                    # 共用部分的顺序必须与桌面菜单完全一致，托盘专用项只放在最前
                    pet_order = [
                        act.text()
                        for act in coord._pet._context_menu.actions()
                        if not act.isSeparator()
                    ]
                    tray_order = [
                        act.text() for act in coord._tray._menu.actions() if not act.isSeparator()
                    ]
                    self.assertEqual(tray_order[1:], pet_order)

                    # 缩放子菜单的壳各自创建，但四个缩放动作是同一批 QAction
                    for owner in (coord._pet._context_menu, coord._tray._menu):
                        self.assertTrue(
                            any("缩放大小" in a.text() for a in owner.actions() if not a.isSeparator())
                        )
                        self.assertTrue(
                            any("形象切换" in a.text() for a in owner.actions() if not a.isSeparator())
                        )
                    pet_scale = self._find_action(coord._pet._context_menu, "缩放大小").menu()
                    tray_scale = self._find_action(coord._tray._menu, "缩放大小").menu()
                    self.assertIsNotNone(pet_scale)
                    self.assertIsNotNone(tray_scale)
                    pet_scale_actions = [a for a in pet_scale.actions() if not a.isSeparator()]
                    tray_scale_actions = [a for a in tray_scale.actions() if not a.isSeparator()]
                    self.assertEqual(len(pet_scale_actions), 4)
                    for pet_action, tray_action in zip(pet_scale_actions, tray_scale_actions):
                        self.assertIs(pet_action, tray_action)

                    # 形象切换同样是两个菜单共用同一批动作
                    pet_style = self._find_action(coord._pet._context_menu, "形象切换").menu()
                    tray_style = self._find_action(coord._tray._menu, "形象切换").menu()
                    self.assertIsNotNone(pet_style)
                    self.assertIsNotNone(tray_style)
                    pet_style_actions = [a for a in pet_style.actions() if not a.isSeparator()]
                    tray_style_actions = [a for a in tray_style.actions() if not a.isSeparator()]
                    self.assertEqual(len(pet_style_actions), 2)
                    for pet_action, tray_action in zip(pet_style_actions, tray_style_actions):
                        self.assertIs(pet_action, tray_action)
                    for removed in ("开机自动启动", "反重力悬浮", "鼠标穿透", "窗口置顶", "互动音效"):
                        self.assertFalse(any(removed in text for text in tray_texts), removed)

                    # 右键「固定（自由漫步）」勾选后停止漫步，再次点击恢复
                    pet_pin = self._find_action(coord._pet._context_menu, "固定（自由漫步）")
                    self.assertIsNone(pet_pin.menu())
                    self.assertTrue(pet_pin.isCheckable())
                    self.assertFalse(pet_pin.isChecked())
                    pet_pin.trigger()
                    app.processEvents()
                    self.assertFalse(coord._settings.auto_walk)
                    self.assertTrue(pet_pin.isChecked())
                    pet_pin.trigger()
                    app.processEvents()
                    self.assertTrue(coord._settings.auto_walk)
                    self.assertFalse(pet_pin.isChecked())

                    # 设置窗口内开启/关闭开机自启，启动快捷方式同步创建与移除
                    pet_settings_act = self._find_action(coord._pet._context_menu, "设置")
                    pet_settings_act.trigger()
                    app.processEvents()

                    win = coord._settings_win
                    self.assertIsNotNone(win)
                    assert win is not None
                    self.assertTrue(win.isVisible())
                    self.assertTrue(win._status_timer.isActive())
                    self.assertTrue(hasattr(win, "_chk_autostart"))
                    self.assertFalse(win._chk_autostart.isChecked())
                    self.assertIsNotNone(win._search_edit)
                    self.assertEqual(len(win._summary_rows), 25)

                    win._chk_autostart.setChecked(True)
                    self.assertTrue(win.has_unsaved_changes())
                    win._confirm_btn.click()
                    app.processEvents()
                    self.assertTrue(SettingsService.is_startup_enabled())
                    self.assertTrue((startup_dir / "GeminiDesktopPet.lnk").is_file())
                    self.assertTrue(win._chk_autostart.isChecked())

                    win._chk_autostart.setChecked(False)
                    win._confirm_btn.click()
                    app.processEvents()
                    self.assertFalse(SettingsService.is_startup_enabled())
                    self.assertFalse((startup_dir / "GeminiDesktopPet.lnk").exists())

                    win.close()
                    app.processEvents()
                    self.assertFalse(win.isVisible())
                    self.assertFalse(win._status_timer.isActive())

                    win.move(-5000, -5000)
                    win.showMinimized()
                    app.processEvents()

                    tray_settings_act = self._find_action(coord._tray._menu, "设置")
                    tray_settings_act.trigger()
                    app.processEvents()

                    self.assertTrue(win.isVisible())
                    self.assertFalse(win.isMinimized())
                    self.assertTrue(win._status_timer.isActive())
                    avail = (win.screen() or app.primaryScreen()).availableGeometry()
                    self.assertTrue(avail.intersects(win.frameGeometry()))
                finally:
                    coord.gracefully_exit(allow_respawn_watcher=False)
                    app.processEvents()

    def test_doze_mode_shared_menu_and_settings_sync(self) -> None:
        app = self._ensure_app()
        with (
            tempfile.TemporaryDirectory() as tmp_startup,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            startup_dir = Path(tmp_startup)
            fake_settings = PetSettings()
            with (
                patch.dict(os.environ, {"USERPROFILE": tmp_home, "APPDATA": tmp_appdata}),
                patch("services.win32_ipc.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.SettingsService.load", return_value=fake_settings),
                patch("services.settings_service.SettingsService.save", return_value=None),
                patch("services.app_follow_service.AppFollowService.sync_and_apply", return_value=None),
                patch.object(PetApplicationCoordinator, "_start_silent_update_check", return_value=None),
            ):
                coord = PetApplicationCoordinator(app, [], MagicMock())
                try:
                    coord._pet.show()
                    app.processEvents()
                    self.assertFalse(hasattr(coord._pet, "_quick_bar"))

                    pet_doze = self._find_action(coord._pet._context_menu, "打盹模式")
                    tray_doze = self._find_action(coord._tray._menu, "打盹模式")
                    self.assertIs(pet_doze, tray_doze)
                    self.assertTrue(pet_doze.isCheckable())
                    self.assertFalse(pet_doze.isChecked())
                    self.assertFalse(coord._settings.doze_mode)

                    coord.open_settings_window()
                    app.processEvents()
                    win = coord._settings_win
                    assert win is not None
                    self.assertFalse(win._chk_doze.isChecked())

                    # 1. 菜单勾选打盹 -> 进入睡姿，设置窗口与配置同步
                    pet_doze.trigger()
                    app.processEvents()
                    self.assertTrue(coord._settings.doze_mode)
                    self.assertTrue(tray_doze.isChecked())
                    self.assertTrue(win._chk_doze.isChecked())
                    self.assertEqual(coord._pet._resolve_resting_state(), PetState.SLEEPING)
                    self.assertTrue(SettingsService().load().doze_mode)

                    # 2. 设置窗口取消（需确认）-> 菜单勾选同步取消
                    win._chk_doze.setChecked(False)
                    self.assertTrue(win.has_unsaved_changes())
                    win._confirm_btn.click()
                    app.processEvents()
                    self.assertFalse(win.has_unsaved_changes())
                    self.assertIn("保存成功", win._save_feedback_lbl.text())
                    self.assertFalse(coord._settings.doze_mode)
                    self.assertFalse(pet_doze.isChecked())
                    self.assertFalse(SettingsService().load().doze_mode)

                    # 3. 从托盘一侧勾选 -> 设置窗口跟随同步
                    tray_doze.trigger()
                    app.processEvents()
                    self.assertTrue(coord._settings.doze_mode)
                    self.assertTrue(pet_doze.isChecked())
                    self.assertTrue(win._chk_doze.isChecked())
                finally:
                    coord.gracefully_exit(allow_respawn_watcher=False)
                    app.processEvents()

    def test_quota_window_places_beside_head_and_auto_closes(self) -> None:
        app = self._ensure_app()

        class _MockDockedPet:
            def isVisible(self) -> bool:
                return True

            def screen(self):
                return app.primaryScreen()

            def frameGeometry(self) -> QRect:
                return QRect(1600, 420, 320, 460)

            def get_visual_sprite_bounds(self) -> QRect:
                # 人物头部位于 y=620 左右，停靠屏幕右侧
                return QRect(1720, 620, 180, 260)

            def get_visual_obstacle_rects(self) -> list[QRect]:
                # 气泡位于人物头顶上方且向左更宽，不应把额度框推到远端或脚底
                return [
                    self.get_visual_sprite_bounds(),
                    QRect(1460, 495, 430, 112),
                ]

            def get_visual_ui_bounds(self) -> QRect:
                rects = self.get_visual_obstacle_rects()
                return rects[0].united(rects[1])

        pet = _MockDockedPet()
        win = QuotaWindow(AntigravityQuotaService(), anchor_pet=pet)
        try:
            win.show_loading_card()
            app.processEvents()

            shadow = 14
            card_rect = QRect(
                win.x() + shadow,
                win.y() + shadow,
                win.width() - shadow * 2,
                win.height() - shadow * 2,
            )
            sr = pet.get_visual_sprite_bounds()
            # 加载气泡应紧贴人物头部左侧，而非被推至远端或人物脚底
            self.assertLessEqual(abs(sr.left() - card_rect.right()), 16)
            self.assertLess(abs(card_rect.top() - sr.top()), 48)
            for obs in pet.get_visual_obstacle_rects():
                self.assertFalse(card_rect.intersects(obs))

            # 启用自动关闭后，拉取完成即启动定时器并能按期自动退出
            win.set_auto_close_enabled(True)
            win._auto_close_timer.setInterval(40)
            win.apply_snapshot(_sample_snapshot())
            app.processEvents()
            self.assertTrue(win.isVisible())
            self.assertTrue(win._auto_close_timer.isActive())

            deadline_spins = 0
            while win.isVisible() and deadline_spins < 30:
                app.processEvents()
                QApplication.sendPostedEvents()
                import time

                time.sleep(0.02)
                deadline_spins += 1
            self.assertFalse(win.isVisible())
        finally:
            win.close()
            win.deleteLater()
            app.processEvents()

    def test_coordinator_toggle_quota_enables_auto_close(self) -> None:
        app = self._ensure_app()
        with (
            tempfile.TemporaryDirectory() as tmp_startup,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            startup_dir = Path(tmp_startup)
            fake_settings = PetSettings(docked_side=DockSide.RIGHT)
            with (
                patch.dict(os.environ, {"USERPROFILE": tmp_home, "APPDATA": tmp_appdata}),
                patch("services.win32_ipc.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.SettingsService.load", return_value=fake_settings),
                patch("services.settings_service.SettingsService.save", return_value=None),
                patch("services.app_follow_service.AppFollowService.sync_and_apply", return_value=None),
                patch.object(PetApplicationCoordinator, "_start_silent_update_check", return_value=None),
                patch("views.quota_window.QuotaWindow.start_refresh", return_value=None),
            ):
                coord = PetApplicationCoordinator(app, [], MagicMock())
                try:
                    coord.toggle_quota_window()
                    app.processEvents()
                    self.assertIsNotNone(coord._quota_win)
                    assert coord._quota_win is not None
                    self.assertTrue(coord._quota_win._auto_close_enabled)
                finally:
                    coord.gracefully_exit(allow_respawn_watcher=False)
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()

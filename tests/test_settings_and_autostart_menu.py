"""偏好设置打开、菜单第一层开机启动项与额度弹窗定位/自动关闭行为测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QCheckBox

from app import PetApplicationCoordinator
from models.pet_settings import DockSide, PetSettings, PetStyleMode
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

                    pet_autostart = self._find_action(coord._pet._context_menu, "开机自动启动")
                    tray_autostart = self._find_action(coord._tray._menu, "开机自动启动")
                    self.assertIsNone(pet_autostart.menu())
                    self.assertIsNone(tray_autostart.menu())
                    self.assertTrue(pet_autostart.isCheckable())
                    self.assertTrue(tray_autostart.isCheckable())
                    self.assertFalse(pet_autostart.isChecked())
                    self.assertFalse(tray_autostart.isChecked())

                    # 通过宠物右键菜单开启开机自启，托盘菜单与启动快捷方式应同步更新
                    pet_autostart.trigger()
                    app.processEvents()
                    self.assertTrue(SettingsService.is_startup_enabled())
                    self.assertTrue((startup_dir / "GeminiDesktopPet.lnk").is_file())
                    self.assertTrue(pet_autostart.isChecked())
                    self.assertTrue(tray_autostart.isChecked())

                    # 通过托盘菜单关闭开机自启，两处菜单与快捷方式同步移除
                    tray_autostart.trigger()
                    app.processEvents()
                    self.assertFalse(SettingsService.is_startup_enabled())
                    self.assertFalse((startup_dir / "GeminiDesktopPet.lnk").exists())
                    self.assertFalse(pet_autostart.isChecked())
                    self.assertFalse(tray_autostart.isChecked())

                    # 验证打开偏好设置、移除开机启动复选框、关闭后再次从托盘拉起及最小化/离屏恢复
                    pet_settings_act = self._find_action(coord._pet._context_menu, "偏好设置")
                    pet_settings_act.trigger()
                    app.processEvents()

                    win = coord._settings_win
                    self.assertIsNotNone(win)
                    assert win is not None
                    self.assertTrue(win.isVisible())
                    self.assertTrue(win._status_timer.isActive())
                    self.assertFalse(hasattr(win, "_chk_autostart"))
                    self.assertFalse(
                        any("开机自动启动" in chk.text() for chk in win.findChildren(QCheckBox))
                    )

                    win.close()
                    app.processEvents()
                    self.assertFalse(win.isVisible())
                    self.assertFalse(win._status_timer.isActive())

                    win.move(-5000, -5000)
                    win.showMinimized()
                    app.processEvents()

                    tray_settings_act = self._find_action(coord._tray._menu, "偏好设置")
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

    def test_doze_mode_first_level_menus_quick_bar_and_settings_sync(self) -> None:
        app = self._ensure_app()
        with (
            tempfile.TemporaryDirectory() as tmp_startup,
            tempfile.TemporaryDirectory() as tmp_home,
            tempfile.TemporaryDirectory() as tmp_appdata,
        ):
            startup_dir = Path(tmp_startup)
            with (
                patch.dict(os.environ, {"USERPROFILE": tmp_home, "APPDATA": tmp_appdata}),
                patch("services.win32_ipc.get_windows_startup_dir", return_value=startup_dir),
                patch("services.settings_service.get_windows_startup_dir", return_value=startup_dir),
                patch("services.app_follow_service.AppFollowService.sync_and_apply", return_value=None),
                patch.object(PetApplicationCoordinator, "_start_silent_update_check", return_value=None),
            ):
                coord = PetApplicationCoordinator(app, [], MagicMock())
                try:
                    coord._pet.show()
                    app.processEvents()

                    # 原「隐藏待机」已由一级「打盹模式」取代，快捷栏也替换为打盹按钮
                    self.assertFalse(
                        any("隐藏待机" in a.text() for a in coord._pet._context_menu.actions() if not a.isSeparator())
                    )
                    self.assertFalse(
                        any("隐藏待机" in a.text() for a in coord._tray._menu.actions() if not a.isSeparator())
                    )
                    self.assertFalse(hasattr(coord._pet, "_quick_pat_btn"))
                    self.assertFalse(
                        any("摸摸猫耳" in btn.text() for btn in coord._pet._quick_bar.findChildren(type(coord._pet._quick_doze_btn)))
                    )

                    pet_doze = self._find_action(coord._pet._context_menu, "打盹模式")
                    tray_doze = self._find_action(coord._tray._menu, "打盹模式")
                    self.assertIsNone(pet_doze.menu())
                    self.assertIsNone(tray_doze.menu())
                    self.assertTrue(pet_doze.isCheckable())
                    self.assertTrue(tray_doze.isCheckable())
                    self.assertFalse(coord._settings.doze_mode)
                    self.assertFalse(pet_doze.isChecked())
                    self.assertFalse(tray_doze.isChecked())
                    self.assertEqual(coord._pet._quick_doze_btn.text(), "💤 打盹模式")

                    coord.open_settings_window()
                    app.processEvents()
                    win = coord._settings_win
                    assert win is not None
                    self.assertFalse(win._chk_doze.isChecked())

                    # 1. 悬浮快捷栏点击开启 -> 同步右键菜单、托盘菜单、偏好设置并持久化
                    coord._pet._quick_doze_btn.click()
                    app.processEvents()
                    self.assertTrue(coord._settings.doze_mode)
                    self.assertTrue(pet_doze.isChecked())
                    self.assertTrue(tray_doze.isChecked())
                    self.assertTrue(win._chk_doze.isChecked())
                    self.assertEqual(coord._pet._quick_doze_btn.text(), "💤 打盹中")
                    self.assertEqual(coord._pet._resolve_resting_state(), PetState.SLEEPING)
                    self.assertTrue(SettingsService().load().doze_mode)

                    # 2. 偏好设置取消勾选（需点击确认修改后才写入配置并同步两级菜单与快捷栏）
                    win._chk_doze.setChecked(False)
                    app.processEvents()
                    self.assertTrue(win.has_unsaved_changes())
                    self.assertTrue(coord._settings.doze_mode)
                    self.assertTrue(SettingsService().load().doze_mode)

                    win._confirm_btn.click()
                    app.processEvents()
                    self.assertFalse(win.has_unsaved_changes())
                    self.assertIn("保存成功", win._save_feedback_lbl.text())
                    self.assertEqual(coord._settings.style_mode, PetStyleMode.CHIBI)
                    self.assertFalse(coord._settings.doze_mode)
                    self.assertFalse(pet_doze.isChecked())
                    self.assertFalse(tray_doze.isChecked())
                    self.assertEqual(coord._pet._quick_doze_btn.text(), "💤 打盹模式")
                    self.assertFalse(SettingsService().load().doze_mode)

                    # 3. 托盘一级菜单开启、右键一级菜单关闭 -> 全链路保持一致
                    tray_doze.trigger()
                    app.processEvents()
                    self.assertTrue(coord._settings.doze_mode)
                    self.assertTrue(pet_doze.isChecked())
                    self.assertTrue(win._chk_doze.isChecked())

                    pet_doze.trigger()
                    app.processEvents()
                    self.assertFalse(coord._settings.doze_mode)
                    self.assertFalse(tray_doze.isChecked())
                    self.assertFalse(win._chk_doze.isChecked())
                finally:
                    coord.gracefully_exit(allow_respawn_watcher=False)
                    app.processEvents()

    def test_quota_window_places_beside_head_and_auto_closes(self) -> None:
        app = self._ensure_app()

        class _MockDockedPetWithQuickBar:
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
                # 快捷栏位于人物头顶上方且向左更宽，不应把额度框推到远端或脚底
                return [
                    self.get_visual_sprite_bounds(),
                    QRect(1460, 495, 430, 112),
                ]

            def get_visual_ui_bounds(self) -> QRect:
                rects = self.get_visual_obstacle_rects()
                return rects[0].united(rects[1])

        pet = _MockDockedPetWithQuickBar()
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
            # 加载气泡应紧贴人物头部左侧，而非被推至快捷栏外或人物脚底
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

"""QuotaWindow 与 PetWindow 布局、滚动条、溢出裁剪与悬停/待机行为验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

if os.name != "nt" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from models.pet_settings import DockSide, PetSettings
from services.antigravity_quota_service import (
    AntigravityQuotaService,
    QuotaBucketInfo,
    QuotaGroupInfo,
    QuotaSnapshot,
)
from services.gemini_chat_service import GeminiChatService
from services.settings_service import SettingsService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow
from views.quota_window import QuotaWindow


def _ensure_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class TestWindowLayoutFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_qapp()

    def test_quota_window_no_unwanted_scrollbars_or_clipped_meta(self) -> None:
        svc = AntigravityQuotaService()
        win = QuotaWindow(svc)
        now = datetime.now(timezone.utc)
        snap = QuotaSnapshot(
            success=True,
            error_message=None,
            agy_path="C:/Users/demo/AppData/Local/agy/bin/agy.exe",
            active_cli_model="Gemini 4 Argon (High)",
            remaining_credits=0,
            upgrade_uri=None,
            overview_description=None,
            fetched_at=now,
            groups=[
                QuotaGroupInfo(
                    name="Gemini Models",
                    buckets=[
                        QuotaBucketInfo("g5h", "Gemini 4 Argon", "5h", 0.757, now + timedelta(hours=3)),
                        QuotaBucketInfo("gwk", "Gemini 4 Argon", "weekly", 0.91, now + timedelta(days=4)),
                    ],
                ),
                QuotaGroupInfo(
                    name="Claude and GPT models",
                    buckets=[
                        QuotaBucketInfo("c5h", "Claude / GPT", "5h", 0.88, now + timedelta(hours=4)),
                    ],
                ),
            ],
        )
        win.apply_snapshot(snap)
        win.show()
        self._app.processEvents()

        # 正常 3 项额度列表应完整展开，不出现水平或垂直滚动条
        self.assertFalse(win._scroll.horizontalScrollBar().isVisible())
        self.assertFalse(win._scroll.verticalScrollBar().isVisible())
        self.assertGreaterEqual(win._scroll.height(), win._content_container.sizeHint().height())
        self.assertLess(win._meta_label.geometry().right(), win._copy_btn.geometry().left())
        self.assertIn("Gemini 4 Argon (High)", win._meta_label.text())

        # 超长模型名应平滑省略，不与复制按钮重叠
        long_snap = QuotaSnapshot(
            success=True,
            error_message=None,
            agy_path=snap.agy_path,
            active_cli_model="Gemini 4 Argon Ultra Experimental Extended Long Name (High)",
            remaining_credits=120,
            upgrade_uri=None,
            overview_description=None,
            fetched_at=now,
            groups=snap.groups,
        )
        win.apply_snapshot(long_snap)
        self._app.processEvents()
        self.assertIn("…", win._meta_label.text())
        self.assertLess(win._meta_label.geometry().right(), win._copy_btn.geometry().left())

        # 极多分组触发动态高度上限时仅开启垂直滚动条，水平滚动条始终禁用
        many_buckets = [
            QuotaBucketInfo(f"b{i}", f"Model {i}", "5h", 0.5, now + timedelta(hours=2))
            for i in range(12)
        ]
        overflow_snap = QuotaSnapshot(
            success=True,
            error_message=None,
            agy_path=snap.agy_path,
            active_cli_model="Gemini 4 Argon",
            remaining_credits=10,
            upgrade_uri=None,
            overview_description=None,
            fetched_at=now,
            groups=[QuotaGroupInfo(name="Overflow Models", buckets=many_buckets)],
        )
        win.apply_snapshot(overflow_snap)
        self._app.processEvents()
        expected_max = max(240, min(620, win._screen_available_geometry().height() - 160))
        self.assertFalse(win._scroll.horizontalScrollBar().isVisible())
        self.assertEqual(win._scroll.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.assertEqual(win._scroll.height(), expected_max)
        win.close()
        win.deleteLater()
        self._app.processEvents()

    def test_pet_window_quick_bar_never_clips_and_standby_close_preserves_coords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_svc = SettingsService(tmp)
            sound = SoundService()
            sprites = SpriteManager()
            sys_monitor = SystemMonitorService()
            chat = GeminiChatService()

            for scale, dock in ((1.0, DockSide.NONE), (0.6, DockSide.RIGHT), (1.3, DockSide.NONE)):
                settings = PetSettings(
                    scale=scale,
                    docked_side=dock,
                    show_speech_bubble=True,
                    auto_walk=False,
                    last_left=420.0,
                    last_top=260.0,
                )
                pet = PetWindow(settings, settings_svc, sprites, sound, sys_monitor, chat)
                pet.show()
                pet._is_hovering = True
                pet._quick_bar.setVisible(True)
                pet._is_chat_open = True
                pet._chat_input_panel.setVisible(True)
                pet._bubble_text_lbl.setText(
                    "你好呀主人！这是一段稍微长一点的气泡测试文本，用来验证聊天输入框开启时高度不会挤压立绘或裁剪快捷按钮。"
                )
                pet._speech_bubble.setVisible(True)
                pet.apply_settings_to_window()
                self._app.processEvents()

                self.assertLessEqual(pet._quick_bar.width(), pet._stage_top_panel.width())
                self.assertLessEqual(pet._stage_top_panel.width(), pet.width())
                for btn in pet._quick_bar.findChildren(QPushButton):
                    self.assertGreaterEqual(btn.geometry().left(), 0)
                    self.assertLessEqual(btn.geometry().right(), pet._quick_bar.width() - 1)

                # 验证透明区域坐标（2, 2）不会让快捷栏误判仍处于悬停状态
                self.assertFalse(pet._get_local_sprite_rect().contains(2, 2))
                pet.close()
                pet.deleteLater()
                self._app.processEvents()

            # 验证待机态（从未 show 的 PetWindow）关闭不会把 (0, 0) 写入配置文件
            standby_settings = PetSettings(last_left=777.0, last_top=333.0)
            settings_svc.save(standby_settings)
            standby_pet = PetWindow(standby_settings, settings_svc, sprites, sound, sys_monitor, chat)
            self.assertFalse(standby_pet._first_shown)
            standby_pet.close()
            standby_pet.deleteLater()
            self._app.processEvents()
            reloaded = settings_svc.load()
            self.assertAlmostEqual(reloaded.last_left or 0.0, 777.0, places=1)
            self.assertAlmostEqual(reloaded.last_top or 0.0, 333.0, places=1)


if __name__ == "__main__":
    unittest.main()

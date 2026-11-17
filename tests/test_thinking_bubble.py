"""长思维气泡完整展示与点击后持续阅读。"""

from unittest.mock import Mock

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from models.pet_settings import PetSettings
from services.antigravity_chat_collector import SyncEvent
from services.gemini_chat_service import GeminiChatService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow


def test_long_thinking_click_keeps_updates_without_final_bubble(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = PetSettings(auto_walk=False, chat_sync_enabled=False)
    service = Mock(settings_path=tmp_path / "settings.json")
    pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), GeminiChatService())
    try:
        pet.show()
        text = "完整思维内容，保留每一行。\n" * 700 + "末尾标记"
        pet._show_sync_event(SyncEvent("system", text, "thinking"))
        app.processEvents()
        assert pet._bubble_text_lbl.text() == "💭 " + text
        bar = pet._bubble_scroll.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(100)
        QTest.mouseClick(pet._bubble_text_lbl, Qt.MouseButton.LeftButton, pos=QPoint(10, 110))
        assert pet._sync_thinking_pinned
        assert not pet._bubble_timer.isActive()
        position = bar.value()

        pet._show_sync_event(SyncEvent("system", text + "\n思维继续更新", "thinking"))
        app.processEvents()
        assert pet._bubble_text_lbl.text().endswith("思维继续更新")
        assert bar.value() == position
        assert not pet._bubble_timer.isActive()

        # 最终回复和闲聊不得覆盖阅读中的思维气泡。
        pet._show_sync_event(SyncEvent("assistant", "最终回答", "message"))
        pet.show_bubble("闲聊", 3.0)
        pet.eventFilter(pet._speech_bubble, QEvent(QEvent.Type.Leave))
        pet._on_bubble_resize_finished(pet._speech_bubble.geometry())
        pet._on_bubble_timeout()
        assert pet._bubble_text_lbl.text().endswith("思维继续更新")
        assert pet._speech_bubble.isVisible()
        assert not pet._bubble_timer.isActive()

        pet._close_bubble_click()
        assert not pet._sync_thinking_pinned
        pet._show_sync_event(SyncEvent("assistant", "后续正常回复", "message"))
        assert pet._bubble_text_lbl.text() == "后续正常回复"
        assert pet._bubble_timer.isActive()
    finally:
        pet.close()

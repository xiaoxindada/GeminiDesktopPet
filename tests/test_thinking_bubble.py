"""同步气泡滚动位置、思维阅读与关闭计时。"""

from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSlider, QApplication

from models.pet_settings import PetSettings
from services.antigravity_chat_collector import SyncEvent
from services.gemini_chat_service import GeminiChatService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow


def test_sync_bubble_uses_normal_bubble_style_and_preserves_scroll_position(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = PetSettings(auto_walk=False, chat_sync_enabled=False, show_speech_bubble=False)
    service = Mock(settings_path=tmp_path / "settings.json")
    pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), GeminiChatService())
    try:
        pet.show()
        pet._frame_timer.stop()
        bubble = pet._speech_bubble
        bar = pet._bubble_scroll.verticalScrollBar()

        pet.show_bubble("开始思考", 3.0, force=True)
        app.processEvents()
        normal_short_size = bubble.size()

        pet._show_sync_event(SyncEvent("assistant", "开始思考", "thinking"))
        app.processEvents()
        assert bubble.isVisible()
        assert not pet._chat_messages_panel.isVisible()
        assert bubble.size().height() <= normal_short_size.height() + 8
        assert bar.maximum() == 0

        text = "同步思维仍在继续。\n" * 80
        pet._show_sync_event(SyncEvent("assistant", text, "thinking", replace=True))
        app.processEvents()
        expanded_geometry = bubble.geometry()
        assert expanded_geometry.height() > normal_short_size.height()
        assert bar.isVisible()
        assert bar.value() == bar.minimum() < bar.maximum()

        bar.triggerAction(QAbstractSlider.SliderAction.SliderToMaximum)
        position = bar.value()
        pet._show_sync_event(SyncEvent("assistant", text * 2, "thinking", replace=True))
        app.processEvents()
        assert bubble.geometry() == expanded_geometry
        assert bar.value() == position < bar.maximum()

        pet._show_sync_event(SyncEvent("assistant", text * 2 + "回复末尾", "message"))
        app.processEvents()
        assert bubble.geometry() == expanded_geometry
        assert pet._bubble_text_lbl.text().endswith("回复末尾")
        assert bar.value() == position < bar.maximum()

        pet.show_bubble("普通气泡", 3.0, force=True)
        app.processEvents()
        assert bubble.width() > 240
        assert bar.maximum() == 0
    finally:
        pet.close()


def test_new_turn_user_sync_message_auto_closes_without_waiting_for_thinking(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = PetSettings(auto_walk=False, chat_sync_enabled=False, show_speech_bubble=False)
    service = Mock(settings_path=tmp_path / "settings.json")
    pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), GeminiChatService())
    try:
        pet.show()
        pet._frame_timer.stop()

        # 新一轮用户消息到达时应立刻启动 3 秒空闲关闭倒计时，无需等待思考开始或完成。
        pet._show_sync_event(SyncEvent("user", "新一轮提问", "message"))
        app.processEvents()
        assert pet._speech_bubble.isVisible()
        assert pet._bubble_text_lbl.text() == "你：新一轮提问"
        assert not pet._sync_waiting_for_reply
        assert pet._bubble_timer.isActive()
        assert pet._bubble_timer.interval() == 3000

        QTest.qWait(3150)
        assert not pet._speech_bubble.isVisible()
    finally:
        pet.close()


def test_long_thinking_keeps_reading_position_until_final_reply_idle_timeout(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = PetSettings(auto_walk=False, chat_sync_enabled=False)
    service = Mock(settings_path=tmp_path / "settings.json")
    pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), GeminiChatService())
    try:
        pet.show()
        pet._frame_timer.stop()
        text = "完整思维内容，保留每一行。\n" * 700 + "末尾标记"
        pet._show_sync_event(SyncEvent("system", text, "thinking"))
        app.processEvents()
        assert pet._bubble_text_lbl.text() == "💭 " + text
        assert not pet._bubble_timer.isActive()
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
        pet._on_bubble_timeout()
        assert pet._speech_bubble.isVisible()

        # 等候队列排空后开始倒计时，同时保留阅读中的思维。
        pet._ag_playback.append(SyncEvent("assistant", "最终回答续写", "message", replace=True))
        pet._show_sync_event(SyncEvent("assistant", "最终回答", "message"))
        assert not pet._bubble_timer.isActive()
        pet._play_antigravity_queue()
        pet.show_bubble("闲聊", 3.0)
        assert pet._bubble_text_lbl.text().endswith("思维继续更新")
        timer = pet._bubble_timer
        assert timer.isActive()
        assert timer.interval() == 3000

        QTest.qWait(120)
        previous_remaining = timer.remainingTime()
        viewport = pet._bubble_scroll.viewport()
        point = QPoint(15, 15)
        event = QWheelEvent(
            QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False,
        )
        QApplication.sendEvent(viewport, event)
        assert bar.value() > position
        assert timer.remainingTime() > previous_remaining

        QTest.qWait(120)
        previous_remaining = timer.remainingTime()
        QTest.mouseClick(pet._bubble_text_lbl, Qt.MouseButton.LeftButton, pos=QPoint(10, bar.value() + 10))
        assert timer.remainingTime() > previous_remaining
        QTest.qWait(3150)
        assert not pet._speech_bubble.isVisible()
        assert not pet._sync_thinking_pinned
        pet._show_sync_event(SyncEvent("assistant", "后续正常回复", "message"))
        assert pet._bubble_text_lbl.text() == "后续正常回复"
        assert timer.isActive()
    finally:
        pet.close()


def test_stopping_sync_keeps_bubble_visible_through_settings_refresh(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = PetSettings(auto_walk=False, chat_sync_enabled=False, show_speech_bubble=False)
    service = Mock(settings_path=tmp_path / "settings.json")
    pet = PetWindow(settings, service, SpriteManager(), SoundService(), SystemMonitorService(), GeminiChatService())
    try:
        pet.show()
        pet._frame_timer.stop()
        pet.settings_synced.connect(pet.apply_settings_to_window)
        conversation = Mock(source_id="synthetic", messages=["测试基线"])
        with patch.object(pet._ag_sync_service, "latest_conversation", return_value=conversation), patch.object(
            pet._ag_collector, "watch",
        ):
            pet.set_antigravity_sync_enabled(True, silent=True)

        pet._show_sync_event(SyncEvent("system", "正在思考的示例内容", "thinking"))
        assert pet._speech_bubble.isVisible()
        assert not pet._bubble_timer.isActive()

        pet.set_antigravity_sync_enabled(False)
        assert not settings.chat_sync_enabled
        assert pet._speech_bubble.isVisible()
        assert pet._bubble_text_lbl.text().endswith("正在思考的示例内容")
        assert pet._bubble_timer.isActive()
        assert pet._bubble_timer.interval() == 3000

        pet.set_speech_bubble_enabled(False)
        assert pet._speech_bubble.isVisible()
        assert pet._bubble_timer.isActive()
        QTest.qWait(3150)
        assert not pet._speech_bubble.isVisible()
    finally:
        pet.close()

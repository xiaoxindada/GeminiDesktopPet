"""聊天尺寸挡位在历史栏展开及屏幕限制下仍可完整循环。"""

import tempfile
from unittest.mock import patch

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from models.pet_settings import PetSettings
from services.gemini_chat_service import GeminiChatService
from services.settings_service import SettingsService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow


@pytest.mark.parametrize("history_expanded", [True, False])
@pytest.mark.parametrize("screen_size", [(1600, 1000), (600, 500)])
def test_size_button_cycles_with_history_and_screen_limits(history_expanded, screen_size):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(auto_walk=False, chat_offline_mode=True, sound_enabled=False)
        service = SettingsService(tmp)
        pet = PetWindow(settings, service, SpriteManager(), SoundService(),
                        SystemMonitorService(), GeminiChatService())
        try:
            with patch.object(pet, "_get_work_area", return_value=QRect(0, 0, *screen_size)):
                pet.open_chat_dialog()
                app.processEvents()
                assert pet._history_list.isVisible()
                assert pet._chat_size_btn.text().endswith("大")
                pet._toggle_chat_history(history_expanded)
                for _ in range(2):
                    sizes = []
                    for name, width, height in pet._CHAT_WINDOW_SIZE_PRESETS:
                        pet._chat_size_btn.click()
                        app.processEvents()
                        assert pet._chat_size_btn.text().endswith(name)
                        saved = service.load()
                        assert (saved.chat_width, saved.chat_height) == (width, height)
                        assert pet._speech_bubble.size() == pet._compute_bubble_target_size()
                        assert pet._history_list.isVisible() == history_expanded
                        sizes.append((pet._speech_bubble.width(), pet._speech_bubble.height()))
                    if screen_size == (1600, 1000):
                        assert len(set(sizes)) == 3
                pet._toggle_chat_history(False)
                pet._close_bubble_click()
                pet.open_chat_dialog()
                app.processEvents()
                assert pet._history_list.isVisible()
                assert pet._history_btn.isChecked()
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()

"""本地会话存储、原聊天窗口历史浏览与续聊回归测试。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from models.pet_settings import PetSettings
from services.chat_history_service import ChatHistoryService
from services.gemini_chat_service import GeminiChatService, ChatReply
from services.settings_service import SettingsService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow


def test_archive_restart_and_late_reply_stay_in_original_session():
    with tempfile.TemporaryDirectory() as tmp:
        archive = ChatHistoryService(Path(tmp))
        first = archive.session_id
        archive.append("user", "示例：如何安排阅读计划？")
        archive.new_session()
        second = archive.session_id
        archive.append("user", "另一段会话")
        archive.append("assistant", "先选择一本书。", session_id=first)
        restarted = ChatHistoryService(Path(tmp))
        assert len(restarted.sessions()) == 2
        assert restarted.load(first)["messages"][-1]["text"] == "先选择一本书。"
        assert len(restarted.load(second)["messages"]) == 1
        (archive.folder / ("a" * 32 + ".json")).write_text("broken", encoding="utf-8")
        assert len(restarted.sessions()) == 2
        assert not list(archive.folder.glob("*.tmp"))


def test_restored_context_is_sent_to_official_api_and_new_chat_resets_it():
    chat = GeminiChatService()
    chat.restore_conversation([
        {"role": "user", "text": "我想阅读科幻小说"},
        {"role": "assistant", "text": "可以先选一篇短篇。"},
    ])
    client = MagicMock()
    client.__enter__.return_value = client
    client.models.generate_content.return_value = SimpleNamespace(text="可以每天读十页。")
    client.interactions.create.return_value = SimpleNamespace(id="demo", output_text="新的回答")
    settings = PetSettings(gemini_api_key="demo-api-key")
    with patch("google.genai.Client", return_value=client):
        assert chat.ask("这个计划怎么安排？", settings).text == "可以每天读十页。"
        contents = client.models.generate_content.call_args.kwargs["contents"]
        assert [c["role"] for c in contents] == ["user", "model", "user"]
        assert contents[0]["parts"][0]["text"] == "我想阅读科幻小说"
        client.interactions.create.assert_not_called()
        chat.reset_conversation()
        chat.ask("这是一个新话题", settings)
        assert "previous_interaction_id" not in client.interactions.create.call_args.kwargs
        assert not chat._api_fallback_history


def test_history_uses_existing_chat_cards_path_and_new_session():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(auto_walk=False, chat_offline_mode=True, sound_enabled=False)
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, SettingsService(tmp), SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        try:
            old = pet._chat_archive.session_id
            task_dir = Path(tmp) / "demo-task"
            task_dir.mkdir()
            with patch("views.pet_window.QFileDialog.getExistingDirectory", return_value=str(task_dir)):
                pet._task_folder_btn.click()
            assert pet._task_folder_path.text() == str(task_dir.resolve())
            assert pet._chat_archive.load(old)["task_folder"] == str(task_dir.resolve())
            with patch("views.pet_window.QFileDialog.getExistingDirectory", return_value=""):
                pet._task_folder_btn.click()
            assert pet._chat_archive.task_folder == str(task_dir.resolve())
            assert not list(task_dir.iterdir())
            for index in range(65):
                pet._chat_archive.append("user" if index % 2 == 0 else "assistant", f"示例消息 {index}")
            pet._clear_chat_history()
            new = pet._chat_archive.session_id
            assert pet._task_folder_path.text() == ""
            pet._show_chat_history()
            app.processEvents()
            assert pet._speech_bubble.isVisible()
            assert pet._history_list.isVisible()
            assert pet._history_list.window() is pet._speech_bubble
            assert pet._history_list.count() == 2
            assert str(pet._chat_archive.session_path(new)) == pet._chat_path.text()
            for index in range(pet._history_list.count()):
                item = pet._history_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == old:
                    pet._history_list.setCurrentItem(item)
                    break
            app.processEvents()
            assert pet._chat_archive.session_id == old
            assert pet._task_folder_path.text() == str(task_dir.resolve())
            assert pet._chat_archive.load(old)["title"] == "示例消息 0"
            assert len(pet._chat_message_rows) == 65
            assert "示例消息 0" in [label.text() for label in pet._chat_message_rows[0].findChildren(QLabel)]
            assert str(pet._chat_archive.session_path(old)) == pet._chat_path.text()
            assert pet._chat._restored_conversation
            assert pet._chat_input_panel.isVisible()
            assert pet._chat_path_panel.geometry().bottom() < pet._speech_bubble.height()
            assert pet._chat_path.width() < pet._speech_bubble.width()
            assert pet._bubble_scroll.viewport().width() >= 250
            # 回复中禁止换会话，避免工作线程的上下文与选中的会话混合。
            marker = object()
            pet._waiting_chat_workers.add(marker)
            pet._clear_chat_history()
            assert pet._chat_archive.session_id == old
            pet._waiting_chat_workers.clear()
            pet._clear_chat_history()
            assert pet._chat_archive.session_id != old
            assert pet._chat_archive.session_path(old).exists()
            pet._toggle_chat_history(False)
            app.processEvents()
            assert pet._history_list.isHidden()
            assert pet._chat_path_panel.isVisible()
            with patch.object(pet._chat_archive, "append", side_effect=OSError("read only")):
                pet._save_chat_message("user", "保存失败示例")
            assert "保存失败" in pet._chat_save_status.text()
            # 从原输入区发送，确认用户消息与异步回复都落在当前文件。
            pet._chat_input.setText("发送一条示例消息")
            with patch("views.pet_window._ChatWorker.start"):
                pet._submit_chat()
            worker = pet._active_chat_workers[-1]
            assert not pet._chat_input_panel.isEnabled()
            pet._on_chat_reply_ready(ChatReply("示例回复"), worker._target_info, worker)
            messages = pet._chat_archive.load(pet._chat_archive.session_id)["messages"]
            assert [message["text"] for message in messages] == ["发送一条示例消息", "示例回复"]
            assert pet._chat_input_panel.isEnabled()
            assert "已保存" in pet._chat_save_status.text()
            pet._cleanup_chat_worker(worker)
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()


def test_delete_session_removes_file_and_keeps_other_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        archive = ChatHistoryService(Path(tmp))
        archive.append("user", "将被删除的会话")
        doomed = archive.session_id
        archive.new_session()
        archive.append("user", "保留下来的会话")
        kept = archive.session_id
        (archive.folder / (doomed + ".tmp")).write_text("{}", encoding="utf-8")
        archive.delete_session(doomed)
        assert not archive.session_path(doomed).exists()
        assert not (archive.folder / (doomed + ".tmp")).exists()
        assert archive.session_path(kept).exists()
        assert [record["id"] for record in archive.sessions()] == [kept]


def _find_history_close_button(pet, session_id):
    """找到历史列表中指定会话条目的 × 删除按钮。"""
    for index in range(pet._history_list.count()):
        item = pet._history_list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == session_id:
            widget = pet._history_list.itemWidget(item)
            for button in widget.findChildren(QPushButton):
                if button.text() == "×":
                    return button
    raise AssertionError(f"未找到会话 {session_id} 的关闭按钮")


def test_delete_session_button_removes_selected_conversation():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(auto_walk=False, chat_offline_mode=True, sound_enabled=False)
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, SettingsService(tmp), SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        try:
            pet._chat_archive.append("user", "将被删除的会话")
            doomed = pet._chat_archive.session_id
            pet._chat_archive.new_session()
            pet._chat_archive.append("user", "保留的会话")
            kept = pet._chat_archive.session_id
            pet._show_chat_history()
            app.processEvents()
            assert pet._history_list.count() == 2

            # 每个条目右上角都有 × 按钮，点击直接删除，无确认弹窗。
            close_btn = _find_history_close_button(pet, doomed)
            assert close_btn is not None
            close_btn.click()
            app.processEvents()
            assert not pet._chat_archive.session_path(doomed).exists()
            assert pet._chat_archive.session_path(kept).exists()
            ids = {
                pet._history_list.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(pet._history_list.count())
            }
            assert doomed not in ids  # 列表立即刷新，删除项消失
            assert kept in ids
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()


def test_delete_current_session_resets_and_refreshes():
    """删除当前选中的会话后应清空消息、切换新会话并刷新列表。"""
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(auto_walk=False, chat_offline_mode=True, sound_enabled=False)
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, SettingsService(tmp), SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        try:
            pet._chat_archive.append("user", "将被删除的当前会话")
            doomed = pet._chat_archive.session_id
            pet._chat_archive.new_session()
            pet._chat_archive.append("user", "保留的会话")
            kept = pet._chat_archive.session_id
            pet._show_chat_history()
            app.processEvents()
            for index in range(pet._history_list.count()):
                item = pet._history_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == doomed:
                    pet._history_list.setCurrentItem(item)
                    break
            app.processEvents()
            assert pet._chat_archive.session_id == doomed

            _find_history_close_button(pet, doomed).click()
            app.processEvents()
            assert not pet._chat_archive.session_path(doomed).exists()
            assert pet._chat_archive.session_id != doomed
            assert pet._chat_archive.session_path(kept).exists()
            assert not pet._chat_message_rows
            assert "已删除" in pet._bubble_text_lbl.text()
            ids = {
                pet._history_list.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(pet._history_list.count())
            }
            assert doomed not in ids
            assert kept in ids
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()


def test_task_folder_survives_restart_without_moving_conversation_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task = root / "demo-task"
        task.mkdir()
        archive = ChatHistoryService(root / "config")
        archive.set_task_folder(str(task))
        sid = archive.session_id
        restarted = ChatHistoryService(root / "config")
        record = restarted.load(sid)
        assert record["task_folder"] == str(task.resolve())
        assert record["messages"] == []
        assert archive.session_path().parent == root / "config" / "conversations"
        assert not list(task.iterdir())
        archive.append("user", "新任务")
        assert archive.load(sid)["task_folder"] == str(task.resolve())
        assert archive.load(sid)["title"] == "新任务"

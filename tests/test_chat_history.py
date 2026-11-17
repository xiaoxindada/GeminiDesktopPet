"""本地会话存储、原聊天窗口历史浏览与续聊回归测试。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from models.pet_settings import PetSettings
from services.app_follow_service import AppFollowService
from services.chat_history_service import ChatHistoryService
from services.antigravity_chat_collector import SyncEvent
from services.gemini_chat_service import GeminiChatService, ChatReply
from services.settings_service import SettingsService
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow
from views.settings_window import SettingsWindow


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


def test_unsaved_first_chat_does_not_appear_in_history():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(auto_walk=False, chat_offline_mode=True, sound_enabled=False)
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, SettingsService(tmp), SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        try:
            first = pet._chat_archive.session_id
            pet._show_chat_history()
            app.processEvents()
            assert pet._history_list.count() == 0
            assert not pet._chat_archive.session_path().exists()

            pet._save_chat_message("user", "示例问题")
            assert pet._history_list.count() == 1
            assert pet._history_list.item(0).data(Qt.ItemDataRole.UserRole) == first

            pet._clear_chat_history()
            assert pet._history_list.count() == 1
            assert pet._history_list.currentItem() is None
            assert not pet._chat_archive.session_path().exists()
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()


def test_sync_chat_uses_temporary_history_and_mode_switch_restores_regular_chat():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        settings = PetSettings(
            auto_walk=False, chat_offline_mode=True, sound_enabled=False,
            gemini_api_key="demo-api-key",
        )
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, SettingsService(tmp), SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        try:
            pet._save_chat_message("user", "普通会话问题")
            regular_id = pet._chat_archive.session_id
            pet._show_chat_history()
            conversation = SimpleNamespace(source_id="example-source", messages=[object()])
            with (
                patch.object(pet._ag_sync_service, "latest_conversation", return_value=conversation),
                patch.object(pet._ag_collector, "watch"),
            ):
                pet.set_antigravity_sync_enabled(True)

            sync_id = pet._ag_sync_session_id
            assert sync_id is not None
            assert pet._ag_viewing_sync
            assert pet._chat_readonly
            assert pet._history_list.count() == 2
            assert pet._history_list.currentItem().data(Qt.ItemDataRole.UserRole) == sync_id
            assert pet._chat_archive.session_id == regular_id
            assert sync_id not in {record["id"] for record in pet._chat_archive.sessions()}
            sync_row = pet._history_list.itemWidget(pet._history_list.currentItem())
            assert not any(button.objectName() == "ChatDeleteButton"
                           for button in sync_row.findChildren(QPushButton))
            pet._delete_session(sync_id)
            assert pet._ag_mirror_active
            assert pet._ag_sync_session_id == sync_id

            pet._show_sync_event(SyncEvent("assistant", "同步示例回复", "message"))
            assert len(pet._ag_sync_events) == 1
            row = pet._chat_message_rows[-1]
            label = pet._ag_sync_labels[-1]
            pet._show_sync_event(SyncEvent("assistant", "同步示例回复续写", "message", replace=True))
            assert pet._chat_message_rows[-1] is row
            assert pet._ag_sync_labels[-1] is label
            assert label.text() == "同步示例回复续写"
            pet._show_sync_event(SyncEvent("assistant", "思考内容", "thinking"))
            thinking_row = pet._chat_message_rows[-1]
            thinking_label = pet._ag_sync_labels[-1]
            pet._disable_chat_scroll_auto()
            pet._show_sync_event(SyncEvent("assistant", "思考内容续写", "thinking", replace=True))
            assert pet._chat_message_rows[-1] is thinking_row
            assert pet._ag_sync_labels[-1] is thinking_label
            assert thinking_label.text() == "💭 思考内容续写"
            assert not pet._chat_scroll_auto
            assert len(pet._chat_archive.load(regular_id)["messages"]) == 1

            for index in range(pet._history_list.count()):
                item = pet._history_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == regular_id:
                    pet._history_list.setCurrentItem(item)
                    break
            assert pet._ag_mirror_active
            assert not pet._ag_viewing_sync
            assert pet._chat_input_panel.isEnabled()
            pet._history_list.setCurrentRow(0)
            assert pet._ag_viewing_sync

            pet.select_chat_offline_mode(False)
            assert pet._ag_mirror_active
            assert not pet._ag_viewing_sync
            assert not pet._chat_readonly
            assert pet._chat_input_panel.isEnabled()
            assert not settings.chat_offline_mode
            pet.select_chat_offline_mode(True)
            assert settings.chat_offline_mode
            pet._show_sync_event(SyncEvent("user", "新的同步消息", "message"))
            assert len(pet._ag_sync_events) == 3
            assert pet._ag_viewing_sync
            assert pet._history_list.currentItem().data(Qt.ItemDataRole.UserRole) == sync_id
            assert len(pet._chat_archive.load(regular_id)["messages"]) == 1

            pet._history_list.setCurrentRow(1)
            assert not pet._ag_viewing_sync
            worker = MagicMock()
            pet._waiting_chat_workers.add(worker)
            pet._show_sync_event(SyncEvent("assistant", "回复期间的新数据", "message"))
            assert pet._ag_pending_handoff
            assert not pet._ag_viewing_sync
            pet._cleanup_chat_worker(worker)
            assert pet._ag_viewing_sync
            assert pet._history_list.currentItem().data(Qt.ItemDataRole.UserRole) == sync_id
            pet._clear_chat_history()
            assert pet._ag_mirror_active
            assert pet._ag_sync_session_id == sync_id
            assert not pet._ag_viewing_sync

            for index in range(pet._history_list.count()):
                item = pet._history_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == sync_id:
                    pet._history_list.setCurrentItem(item)
                    break
            assert pet._ag_viewing_sync
            pet.set_antigravity_sync_enabled(False)
            assert not pet._ag_viewing_sync
            assert not pet._chat_readonly
            assert pet._history_list.count() == 1
        finally:
            pet.close()
            pet.deleteLater()
            app.processEvents()


def test_menu_sync_preference_persists_until_a_conversation_is_available():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as tmp:
        service = SettingsService(tmp)
        assert service.load().chat_sync_enabled
        service.settings_path.write_text(
            '{"behavior":{"auto_walk":false,"sound_enabled":false}}', encoding="utf-8"
        )
        settings = service.load()
        sound = SoundService()
        sound.enabled = False
        pet = PetWindow(settings, service, SpriteManager(), sound,
                        SystemMonitorService(), GeminiChatService())
        win = SettingsWindow(
            settings=settings,
            settings_service=service,
            app_follow_service=AppFollowService(service),
            on_settings_applied=pet.apply_settings_to_window,
            on_antigravity_sync_changed=pet.apply_antigravity_sync_preference,
        )
        pet.settings_synced.connect(lambda: win.sync_from_settings(force=False))
        try:
            assert pet._act_sync_chat.isChecked()
            assert win._chk_ag_sync.isChecked()
            with patch.object(pet._ag_sync_service, "latest_conversation", return_value=None) as latest:
                pet.show()
                app.processEvents()
                latest.assert_called_once()
                assert not pet._ag_mirror_active

                pet._act_sync_chat.trigger()
                assert not service.load().chat_sync_enabled
                pet._act_sync_chat.trigger()
            assert settings.chat_sync_enabled
            assert service.load().chat_sync_enabled
            assert pet._act_sync_chat.isChecked()
            assert not pet._ag_mirror_active

            conversation = SimpleNamespace(source_id="example-source", messages=[object()])
            pet._ag_next_retry_at = 0.0
            with (
                patch.object(pet, "isVisible", return_value=True),
                patch.object(pet._ag_sync_service, "latest_conversation", return_value=conversation),
                patch.object(pet._ag_collector, "watch"),
            ):
                pet._refresh_antigravity_sync()
            assert pet._ag_mirror_active
            assert service.load().chat_sync_enabled

            # 菜单保存关闭状态后，确认设置窗口的另一项草稿也不能把它重新打开。
            win._chk_sysmon.setChecked(False)
            pet._act_sync_chat.trigger()
            assert not settings.chat_sync_enabled
            assert not service.load().chat_sync_enabled
            assert not pet._act_sync_chat.isChecked()
            assert not pet._ag_mirror_active
            assert not win._chk_ag_sync.isChecked()
            assert win.has_unsaved_changes()
            win._confirm_btn.click()
            assert not service.load().chat_sync_enabled
            assert not service.load().system_monitor_enabled

            win._chk_ag_sync.setChecked(True)
            with (
                patch.object(pet._ag_sync_service, "latest_conversation", return_value=None),
                patch.object(service, "save", wraps=service.save) as save,
            ):
                win._confirm_btn.click()
                save.assert_called_once()
            assert service.load().chat_sync_enabled
            assert pet._act_sync_chat.isChecked()
        finally:
            win.close()
            win.deleteLater()
            pet.close()
            pet.deleteLater()
            app.processEvents()


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
            assert pet._chat_archive.task_folder == str(task_dir.resolve())
            assert pet._chat_archive.load(old)["task_folder"] == str(task_dir.resolve())
            with patch("views.pet_window.QFileDialog.getExistingDirectory", return_value=""):
                pet._task_folder_btn.click()
            assert pet._chat_archive.task_folder == str(task_dir.resolve())
            assert not list(task_dir.iterdir())
            for index in range(65):
                pet._chat_archive.append("user" if index % 2 == 0 else "assistant", f"示例消息 {index}")
            pet._clear_chat_history()
            new = pet._chat_archive.session_id
            assert pet._chat_archive.task_folder == ""
            assert not pet._chat_open_file_btn.isEnabled()
            pet._show_chat_history()
            app.processEvents()
            assert pet._speech_bubble.isVisible()
            assert pet._history_list.isVisible()
            assert pet._history_list.window() is pet._speech_bubble
            assert pet._history_list.count() == 1
            assert not pet._history_list.item(0).text()
            assert str(pet._chat_archive.session_path(new)) == pet._chat_path.text()
            for index in range(pet._history_list.count()):
                item = pet._history_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == old:
                    pet._history_list.setCurrentItem(item)
                    break
            app.processEvents()
            assert pet._chat_archive.session_id == old
            assert pet._chat_archive.task_folder == str(task_dir.resolve())
            assert pet._chat_archive.load(old)["title"] == "示例消息 0"
            assert len(pet._chat_message_rows) == 65
            assert "示例消息 0" in [label.text() for label in pet._chat_message_rows[0].findChildren(QLabel)]
            assert str(pet._chat_archive.session_path(old)) == pet._chat_path.text()
            with patch("views.pet_window.os.startfile") as open_file:
                pet._chat_open_file_btn.click()
                open_file.assert_called_once_with(str(pet._chat_archive.session_path(old)))
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
    """找到历史列表中指定会话的删除按钮。"""
    for index in range(pet._history_list.count()):
        item = pet._history_list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == session_id:
            widget = pet._history_list.itemWidget(item)
            for button in widget.findChildren(QPushButton):
                if button.objectName() == "ChatDeleteButton":
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

            # 每个条目右侧都有删除按钮，点击直接删除，无确认弹窗。
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

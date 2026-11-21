"""本地会话存储与上下文恢复核心单元测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.pet_settings import PetSettings
from services.chat_history_service import ChatHistoryService
from services.gemini_chat_service import GeminiChatService


def test_archive_restart_and_late_reply_stay_in_original_session() -> None:
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


def test_restored_context_is_sent_to_official_api_and_new_chat_resets_it() -> None:
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


def test_delete_session_removes_file_and_keeps_other_sessions() -> None:
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


def test_task_folder_survives_restart_without_moving_conversation_files() -> None:
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

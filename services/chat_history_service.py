"""现有配置目录中的本地会话存档，不保存连接凭据。"""
import json
import uuid
from datetime import datetime
from pathlib import Path


class ChatHistoryService:
    def __init__(self, config_dir: Path):
        self.folder = Path(config_dir) / "conversations"
        self.new_session()

    def new_session(self):
        self.session_id = uuid.uuid4().hex
        self.task_folder = ""

    def session_path(self, session_id=None) -> Path:
        sid = session_id or self.session_id
        if not isinstance(sid, str) or len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
            raise ValueError("无效的会话标识")
        return self.folder / (sid + ".json")

    def load(self, session_id):
        data = json.loads(self.session_path(session_id).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise ValueError("会话文件格式无效")
        for message in data["messages"]:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant") or not isinstance(message.get("text"), str):
                raise ValueError("会话消息格式无效")
        data["id"] = session_id
        if not isinstance(data.get("task_folder", ""), str):
            raise ValueError("任务文件夹格式无效")
        return data

    def delete_session(self, session_id) -> None:
        """删除指定会话的存档文件；删除当前会话后的界面状态由调用方重置。"""
        path = self.session_path(session_id)
        # 连同可能残留的 .tmp 一起清掉，避免留下孤儿临时文件。
        for target in (path, path.with_suffix(".tmp")):
            if target.exists():
                target.unlink()

    def set_task_folder(self, folder: str) -> None:
        selected = Path(folder)
        if not selected.is_dir():
            raise ValueError("请选择存在的文件夹")
        path = self.session_path()
        data = self.load(self.session_id) if path.exists() else {
            "id": self.session_id, "title": "新对话", "messages": [],
        }
        data["task_folder"] = str(selected.resolve())
        data["updated"] = datetime.now().isoformat(timespec="seconds")
        # 目录只作为会话元数据保存，历史文件仍写入原配置目录。
        self._write(path, data)
        self.task_folder = data["task_folder"]

    def append(self, role, text, session_id=None):
        # 回复绑定原会话，避免新建会话后迟到的回复写入错误文件。
        sid = session_id or self.session_id
        path = self.session_path(sid)
        data = self.load(sid) if path.exists() else {"id": sid, "title": text[:50], "messages": []}
        if not data["messages"] and role == "user":
            data["title"] = text[:50]
        data["messages"].append({"role": role, "text": text, "time": datetime.now().isoformat(timespec="seconds")})
        data["updated"] = data["messages"][-1]["time"]
        self._write(path, data)

    def _write(self, path: Path, data: dict) -> None:
        temporary = path.with_suffix(".tmp")
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def sessions(self):
        result = []
        for path in self.folder.glob("*.json"):
            try:
                result.append(self.load(path.stem))
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda item: item.get("updated", ""), reverse=True)

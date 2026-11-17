"""读取 Antigravity 本地会话数据库，生成气泡里实时展示的同步事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Optional

from services.app_logger import AppLogger

_CONVERSATION_DIRS = (
    Path.home() / ".gemini" / "antigravity" / "conversations",
    Path.home() / ".gemini" / "antigravity-cli" / "conversations",
)
# 会话库里的步骤类型：14 是用户输入，15 是助手回复，132 是工具调用。
_TOOL_STEP_TYPE = 132
# 一次追赶的步数上限：落后太多说明中间被大改过，重新全解比逐段拼更稳。
_MAX_CATCHUP_STEPS = 64
# 只读连接的等锁上限：采集跑在界面线程里，宁可这一拍读不到也不能卡住界面。
_READ_TIMEOUT = 0.5


@dataclass(frozen=True)
class SyncedMessage:
    role: str
    text: str
    time: str
    kind: str = "message"


@dataclass(frozen=True)
class SyncedConversation:
    source_id: str
    title: str
    messages: list[SyncedMessage]
    db_path: Path
    mtime_ns: int
    size: int


@dataclass
class ConversationCache:
    """逐步骤的解析结果，让下一次只重解尾部变动的那几行。

    一条助手回复在库里始终是同一行原地 REPLACE，所以「尾部」既可能是追加，
    也可能是最后一行被改写；两种情况都从最后一个已解析的 idx 开始重解。
    """

    path: str
    rows: int  # 已解析的行数，用来判断中间是否被删改
    max_idx: int
    # 每项：(idx, 该步产出的消息, 该步新增的思考文本)；思考文本供全局去重集合重建。
    steps: list[tuple[int, list[SyncedMessage], tuple[str, ...]]] = field(default_factory=list)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 越界")


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, object]]:
    """按 protobuf 线格式遍历字段，只取定长与长度前缀两类。"""
    index = 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field, wire = key >> 3, key & 0x07
        if wire == 0:
            value, index = _read_varint(data, index)
            yield field, wire, value
        elif wire == 2:
            length, index = _read_varint(data, index)
            end = index + length
            if end > len(data):
                return
            yield field, wire, data[index:end]
            index = end
        elif wire == 5:
            yield field, wire, data[index : index + 4]
            index += 4
        elif wire == 1:
            yield field, wire, data[index : index + 8]
            index += 8
        else:
            return


def _path_text(payload: bytes, path: tuple[int, ...]) -> Optional[str]:
    """按字段路径逐层取值并解成 UTF-8 文本。"""
    current: list[bytes] = [payload]
    for field in path:
        next_level: list[bytes] = []
        for data in current:
            for candidate, wire, value in _iter_fields(data):
                if candidate == field and wire == 2 and isinstance(value, bytes):
                    next_level.append(value)
        current = next_level
        if not current:
            return None
    for data in current:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip():
            return text
    return None


def _step_time(payload: bytes) -> str:
    for field, wire, value in _iter_fields(payload):
        if field != 5 or wire != 2 or not isinstance(value, bytes):
            continue
        seconds = 0
        for outer_field, outer_wire, outer_value in _iter_fields(value):
            if outer_field != 1 or outer_wire != 2 or not isinstance(outer_value, bytes):
                continue
            for inner_field, inner_wire, inner_value in _iter_fields(outer_value):
                if inner_field == 1 and inner_wire == 0 and isinstance(inner_value, int):
                    seconds = inner_value
            break
        if seconds:
            try:
                return datetime.fromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return ""
    return ""


def _tool_event(metadata: bytes, payload: bytes, time: str) -> Optional[SyncedMessage]:
    tool_name = _path_text(metadata, (4, 2)) or "tool"
    summary = ""
    args_text = _path_text(metadata, (4, 3))
    if args_text:
        try:
            args = json.loads(args_text)
        except ValueError:
            args = None
        if isinstance(args, dict):
            summary = str(args.get("toolSummary") or args.get("CommandLine") or "").strip()

    result = _path_text(payload, (140, 2, 1)) or ""
    exit_note = ""
    exit_match = re.search(r"exited with code (-?\d+)", result)
    if exit_match:
        exit_note = f" · 退出码 {exit_match.group(1)}"
    detail = ""
    marker = "Command output:"
    if marker in result:
        detail = result.split(marker, 1)[1].strip()

    text = tool_name
    if summary:
        text += f" · {summary}"
    text += exit_note
    if detail:
        text += f"\n{detail}"
    if not text.strip():
        return None
    return SyncedMessage("system", text.strip(), time, "tool")


def _decode_step(
    step_type: object,
    metadata: object,
    payload: object,
    seen_thinking: set[str],
) -> tuple[list[SyncedMessage], list[str]]:
    """解一步；返回（产出的消息，这一步新记入去重集合的思考文本）。"""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return [], []
    raw_payload = bytes(payload)
    raw_metadata = bytes(metadata) if isinstance(metadata, (bytes, bytearray)) else b""
    time = _step_time(raw_payload)
    produced: list[SyncedMessage] = []
    added: list[str] = []
    if step_type == 14:
        text = _path_text(raw_payload, (19, 2)) or _path_text(raw_payload, (19, 3))
        if text and text.strip():
            produced.append(SyncedMessage("user", text.strip(), time))
    elif step_type == 15:
        thinking = _path_text(raw_payload, (20, 3))
        if thinking and thinking.strip():
            # 保留完整思维及换行；按全文去重，避免相同开头的续写被吞掉。
            cleaned = thinking.strip()
            # 同一段思考会在多个步骤里重复出现，只保留首次。
            if cleaned not in seen_thinking:
                seen_thinking.add(cleaned)
                added.append(cleaned)
                produced.append(SyncedMessage("system", cleaned, time, "thinking"))
        answer = _path_text(raw_payload, (20, 1)) or _path_text(raw_payload, (20, 8))
        if answer and answer.strip():
            produced.append(SyncedMessage("assistant", answer.strip(), time))
    elif step_type == _TOOL_STEP_TYPE:
        event = _tool_event(raw_metadata, raw_payload, time)
        if event is not None:
            produced.append(event)
    return produced, added


def _build_conversation(
    path: Path,
    steps: list[tuple[int, list[SyncedMessage], tuple[str, ...]]],
    mtime_ns: int,
    size: int,
) -> SyncedConversation:
    messages: list[SyncedMessage] = []
    for _idx, produced, _added in steps:
        messages.extend(produced)
    first_user = next((message.text for message in messages if message.role == "user"), "")
    title = " ".join(first_user.split())[:40] or "Antigravity 对话"
    return SyncedConversation(path.stem, title, messages, path, mtime_ns, size)


class AntigravityChatSyncService:
    """把 Antigravity 会话库中的对话转成桌宠气泡可展示的事件列表。"""

    def __init__(self, conversation_dirs: Optional[tuple[Path, ...]] = None) -> None:
        self._dirs = conversation_dirs or _CONVERSATION_DIRS

    @staticmethod
    def read_conversation(path: Path, mtime_ns: int = 0, size: int = 0) -> SyncedConversation:
        try:
            stat = path.stat()
            mtime_ns = mtime_ns or stat.st_mtime_ns
            size = size or stat.st_size
        except OSError:
            pass
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=_READ_TIMEOUT)
        try:
            rows = connection.execute(
                "select idx, step_type, metadata, step_payload from steps order by idx"
            ).fetchall()
        finally:
            connection.close()

        seen_thinking: set[str] = set()
        steps: list[tuple[int, list[SyncedMessage], tuple[str, ...]]] = []
        for idx, step_type, metadata, payload in rows:
            try:
                produced, added = _decode_step(step_type, metadata, payload, seen_thinking)
            except ValueError:
                # 半截写入的载荷解不开，跳过这一步，别让整个会话读不出来。
                continue
            steps.append((idx, produced, tuple(added)))
        return _build_conversation(path, steps, mtime_ns, size)

    @staticmethod
    def read_incremental(
        path: Path,
        cache: Optional[ConversationCache] = None,
        mtime_ns: int = 0,
        size: int = 0,
    ) -> tuple[SyncedConversation, ConversationCache]:
        """优先复用缓存，只重解尾部变动的步骤；结构对不上时退回全解。"""
        try:
            stat = path.stat()
            mtime_ns = mtime_ns or stat.st_mtime_ns
            size = size or stat.st_size
        except OSError:
            pass
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=_READ_TIMEOUT)
        try:
            total, max_idx = connection.execute("select count(*), max(idx) from steps").fetchone()
            total = int(total or 0)
            max_idx = int(max_idx or 0)
            reusable = (
                cache is not None
                and cache.path == str(path)
                and bool(cache.steps)
                and total >= cache.rows
                and max_idx >= cache.max_idx
                and total - cache.rows <= _MAX_CATCHUP_STEPS
            )
            if reusable:
                # 从最后一个已解析的 idx 开始重解：它可能就是被原地改写的助手回复。
                start = cache.steps[-1][0]
                rows = connection.execute(
                    "select idx, step_type, metadata, step_payload from steps "
                    "where idx >= ? order by idx",
                    (start,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "select idx, step_type, metadata, step_payload from steps order by idx"
                ).fetchall()
        finally:
            connection.close()

        if not total:
            return (
                _build_conversation(path, [], mtime_ns, size),
                ConversationCache(str(path), 0, 0),
            )

        seen_thinking: set[str] = set()
        if reusable:
            prefix = cache.steps[:-1]
            # 重建「被重解区间之前」的去重集合，否则思考会被重复收进去。
            for _idx, _produced, added in prefix:
                seen_thinking.update(added)
        else:
            prefix = []

        steps = list(prefix)
        for idx, step_type, metadata, payload in rows:
            try:
                produced, added = _decode_step(step_type, metadata, payload, seen_thinking)
            except ValueError:
                # 同 read_conversation：单步损坏只丢这一步。
                continue
            steps.append((idx, produced, tuple(added)))

        return (
            _build_conversation(path, steps, mtime_ns, size),
            ConversationCache(str(path), total, max_idx, steps),
        )

    def _candidates(self) -> list[tuple[int, int, Path]]:
        candidates: list[tuple[int, int, Path]] = []
        for directory in self._dirs:
            try:
                files = list(directory.glob("*.db"))
            except OSError:
                continue
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                candidates.append((stat.st_mtime_ns, stat.st_size, path))
        candidates.sort(reverse=True)
        return candidates

    def newest_path(self) -> Optional[Path]:
        candidates = self._candidates()
        return candidates[0][2] if candidates else None

    def latest_conversation(self) -> Optional[SyncedConversation]:
        for mtime_ns, size, path in self._candidates():
            try:
                conversation = self.read_conversation(path, mtime_ns, size)
            except (sqlite3.Error, OSError, ValueError) as exc:
                AppLogger.warn("ChatSync", f"跳过会话 {path.name}：{exc}")
                continue
            if conversation.messages:
                return conversation
        return None

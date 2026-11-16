"""从 Antigravity 会话库采集增量事件，写进状态叶子。

采集与展示分开：这里只判断「哪条是新的」「哪条在续写」「哪条该压掉」，
气泡怎么排队、显示多久由消费端决定。库结构决定了助手回复是同一行原地续写，
所以增量判断必须同时看条数和末条文本，只看条数会漏掉流式增长。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Optional

from services.antigravity_chat_sync_service import (
    AntigravityChatSyncService,
    ConversationCache,
    SyncedConversation,
    SyncedMessage,
)
from services.app_logger import AppLogger
from services.chat_sync_state import ChatSyncState

_EXIT_CODE = re.compile(r"(?:exited with code|退出码)\s*(-?\d+)", re.IGNORECASE)
_FAILURE_WORDS = ("error", "failed", "denied", "timeout", "timed out", "失败", "拒绝", "超时", "不存在")


@dataclass(frozen=True)
class SyncEvent:
    """一条待展示的同步事件。"""

    role: str
    text: str
    kind: str
    time: str = ""
    replace: bool = False  # True = 同一条消息的续写，消费端应就地刷新


def _keyword_reason(lower: str, text: str) -> str:
    if "timeout" in lower or "timed out" in lower or "超时" in text:
        return "请求超时"
    if "no such file" in lower or "不存在" in text:
        return "文件或路径不存在"
    if "permission" in lower or "denied" in lower or "权限" in text or "拒绝" in text:
        return "权限不足或访问被拒绝"
    return ""


def failure_reason(text: str) -> str:
    """从工具输出里认出失败原因；没有失败迹象返回空串。"""
    lower = text.lower()
    code = _EXIT_CODE.search(text)
    if code is not None:
        if code.group(1) == "0":
            # 返回码为 0 就是成功，输出里出现 error 字样（比如日志正文）不算失败。
            return ""
        return _keyword_reason(lower, text) or f"命令返回码 {code.group(1)}"
    if not any(word in lower for word in _FAILURE_WORDS):
        return ""
    return _keyword_reason(lower, text) or "工具执行异常"


def _signature(messages: list[SyncedMessage]) -> list[tuple[str, str, str]]:
    return [(message.role, message.kind, message.text) for message in messages]


def _common_prefix(
    previous: list[tuple[str, str, str]], current: list[tuple[str, str, str]]
) -> int:
    """两段快照从头开始相同的条数。"""
    limit = min(len(previous), len(current))
    index = 0
    while index < limit and previous[index] == current[index]:
        index += 1
    return index


class AntigravityChatCollector:
    """轮询会话库并把增量事件写进状态叶子。"""

    def __init__(
        self,
        service: AntigravityChatSyncService,
        state: ChatSyncState,
        *,
        follow_interval: int = 25,
        repeat_cooldown: float = 300.0,
        force_interval: int = 40,
        clock=time.monotonic,
    ) -> None:
        self._service = service
        self._state = state
        self._follow_interval = max(1, follow_interval)
        self._force_interval = max(1, force_interval)
        self._cooldown = repeat_cooldown
        self._clock = clock
        self._ticks = 0
        self._path: Optional[str] = None
        # 主库与 -wal 的 (mtime, size) 四元组；WAL 模式下新内容可能还没落进主库。
        self._stamp: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._cache: Optional[ConversationCache] = None
        self._snapshot: list[tuple[str, str, str]] = []
        self._shown: dict[tuple[str, str], float] = {}
        self._last_replace_log = 0.0
        self._last_error_log = 0.0

    @property
    def stats(self) -> str:
        """诊断用：当前绑定的库、基线长度与记账的文件尺寸。"""
        name = Path(self._path).name[:8] if self._path else "-"
        return f"lib={name} snap={len(self._snapshot)} size={self._stamp[1]}"

    @staticmethod
    def _read_stamp(path: str) -> Optional[tuple[int, int, int, int]]:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        try:
            wal = os.stat(path + "-wal")
        except OSError:
            wal = None
        return (
            stat.st_mtime_ns,
            stat.st_size,
            wal.st_mtime_ns if wal else 0,
            wal.st_size if wal else 0,
        )

    def watch(self, conversation: SyncedConversation) -> None:
        """绑定会话并按当前内容建基线：已有历史只作快照，不重新播报。"""
        self._bind(conversation, switched=False)

    def reset(self) -> None:
        self._path = None
        self._stamp = (0, 0, 0, 0)
        self._cache = None
        self._snapshot = []
        self._shown.clear()
        self._ticks = 0
        self._last_replace_log = 0.0
        self._last_error_log = 0.0

    def poll(self) -> None:
        """采集一次；没有变化时只做两次 stat，长时间没动静再强制复核一次。"""
        if self._path is None:
            return
        self._ticks += 1
        if self._ticks % self._follow_interval == 0:
            self._follow_newest()
        stamp = self._read_stamp(self._path)
        if stamp is None:
            return
        # 每 force_interval 拍兜底复核：stat 精度或文件系统缓存异常时仍能追上。
        if stamp == self._stamp and self._ticks % self._force_interval != 0:
            return
        try:
            conversation, cache = AntigravityChatSyncService.read_incremental(
                Path(self._path), self._cache
            )
        except (sqlite3.Error, OSError, ValueError) as exc:
            # 高频轮询下被写锁挡住是常事，同一条错误每 10 秒只记一次，别把日志刷满。
            now = self._clock()
            if now - self._last_error_log >= 10.0:
                self._last_error_log = now
                AppLogger.warn("ChatSync", f"采集会话失败：{exc}")
            return
        self._stamp = stamp
        self._cache = cache
        self._publish(conversation)

    def _bind(
        self,
        conversation: SyncedConversation,
        *,
        switched: bool,
        cache: Optional[ConversationCache] = None,
    ) -> None:
        path = conversation.db_path
        if cache is None:
            # 建基线时顺手把逐步骤缓存一起建好，之后流式刷新就只解尾部。
            try:
                conversation, cache = AntigravityChatSyncService.read_incremental(path)
            except (sqlite3.Error, OSError, ValueError):
                cache = None
        self._path = str(path)
        self._cache = cache
        self._stamp = self._read_stamp(self._path) or (
            conversation.mtime_ns,
            conversation.size,
            0,
            0,
        )
        self._snapshot = _signature(conversation.messages)
        self._state.write(
            "status",
            {
                "source_id": conversation.source_id,
                "title": conversation.title,
                "switched": switched,
            },
        )

    def _follow_newest(self) -> None:
        """在 Antigravity 里新建对话后自动切到最新一份。"""
        newest = self._service.newest_path()
        if newest is None or str(newest) == self._path:
            return
        try:
            conversation, cache = AntigravityChatSyncService.read_incremental(newest)
        except (sqlite3.Error, OSError, ValueError):
            return
        if not conversation.messages:
            return
        self._bind(conversation, switched=True, cache=cache)

    def _publish(self, conversation: SyncedConversation) -> None:
        messages = conversation.messages
        if not messages:
            return
        current = _signature(messages)
        previous = self._snapshot
        if current == previous:
            return

        common = _common_prefix(previous, current)
        if len(current) > len(previous) and common >= len(previous) - 1:
            # 追加：从第一个不同处开始播（末条可能刚好续写完成），界面自己排队。
            self._snapshot = current
            fresh = [message for message in messages[common:] if self._allow(message)]
            if fresh:
                AppLogger.info(
                    "ChatSync",
                    f"采集到新增 {len(fresh)} 条（{len(previous)}→{len(current)}）",
                )
                self._emit([self._event(message) for message in fresh], replace=False)
            return

        if len(current) == len(previous) and common == len(previous) - 1:
            # 条数不变、只有末条文本变了 = 助手在原地续写，就地刷新同一条。
            self._snapshot = current
            newest = messages[-1]
            if self._allow(newest):
                now = self._clock()
                if now - self._last_replace_log >= 2.0:
                    self._last_replace_log = now
                    AppLogger.info("ChatSync", f"助手续写中（{len(newest.text)} 字）")
                self._emit([self._event(newest, replace=True)], replace=True)
            return

        # 结构对不上（会话被重写或切换）：重建基线，只补最后一条，不重放历史。
        AppLogger.info("ChatSync", f"会话结构变化，重建基线（{len(previous)}→{len(current)}）")
        self._snapshot = current
        newest = messages[-1]
        if self._allow(newest):
            self._emit([self._event(newest)], replace=False)

    def _emit(self, items: list[SyncEvent], *, replace: bool) -> None:
        self._state.write("events", {"items": items, "replace": replace})

    def _allow(self, message: SyncedMessage) -> bool:
        """同一句提示在冷却期内不重复弹；文本一变就是新内容，自然放行。"""
        key = (message.kind, message.text)
        now = self._clock()
        last = self._shown.get(key)
        if last is not None and now - last < self._cooldown:
            return False
        if len(self._shown) > 200:
            self._shown = {k: v for k, v in self._shown.items() if now - v < self._cooldown}
        self._shown[key] = now
        return True

    @staticmethod
    def _event(message: SyncedMessage, *, replace: bool = False) -> SyncEvent:
        kind, text = message.kind, message.text
        if kind == "tool":
            reason = failure_reason(text)
            if reason:
                kind, text = "error", f"{text}\n原因：{reason}"
        return SyncEvent(role=message.role, text=text, kind=kind, time=message.time, replace=replace)

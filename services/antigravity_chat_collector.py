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
import threading
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
    source_id: str = ""    # 所属会话标识，用于多对话并发工作状态追踪
    in_progress: bool = False  # True = 该步骤仍在生成/执行中（如流式回复尚未落盘 DONE）
    stopped: bool = False      # True = 该会话已被取消/异常终止，应回归常态而非保持工作态


@dataclass
class _PeerSession:
    """除当前主播报会话外的并发工作会话追踪状态。"""

    path: str
    source_id: str
    stamp: tuple[int, int, int, int]
    cache: Optional[ConversationCache]
    snapshot: list[tuple[str, str, str]]


def _is_conversation_working(conversation: SyncedConversation) -> bool:
    """优先依据会话库步骤执行状态判定是否工作中，兼容无步骤状态信息的精简会话。"""
    if conversation.has_running_step or conversation.is_working:
        return True
    return False


def _is_recent_message_time(messages: list[SyncedMessage], max_age_sec: float = 300.0) -> bool:
    """判断末条消息时间是否为近期写入，防止旧会话仅因 WAL checkpoint 变更 mtime 被误判为工作中。"""
    if not messages:
        return False
    raw_time = (messages[-1].time or "").strip()
    if not raw_time:
        return True
    try:
        from datetime import datetime

        dt = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S")
        age = abs(time.time() - dt.timestamp())
        return age <= max_age_sec
    except (ValueError, OSError, OverflowError):
        return True


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
        scan_interval: int = 5,
        repeat_cooldown: float = 300.0,
        force_interval: int = 40,
        idle_work_timeout: float = 20.0,
        clock=time.monotonic,
    ) -> None:
        self._service = service
        self._state = state
        self._follow_interval = max(1, follow_interval)
        self._scan_interval = max(1, min(scan_interval, self._follow_interval))
        self._force_interval = max(1, force_interval)
        self._idle_work_timeout = max(1.0, float(idle_work_timeout))
        self._cooldown = repeat_cooldown
        self._clock = clock
        self._lock = threading.RLock()
        self._ticks = 0
        self._path: Optional[str] = None
        self._source_id = ""
        # 主库与 -wal 的 (mtime, size) 四元组；WAL 模式下新内容可能还没落进主库。
        self._stamp: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._cache: Optional[ConversationCache] = None
        self._snapshot: list[tuple[str, str, str]] = []
        # 仅追踪建基线后产生新写入的并发会话，避免把历史残留会话误判为工作中。
        self._known_stamps: dict[str, tuple[int, int, int, int]] = {}
        self._known_progress: dict[str, tuple[int, int]] = {}
        self._peers: dict[str, _PeerSession] = {}
        self._working_sessions: set[str] = set()
        # 记录各会话最近一次确认存在运行步骤或写入变动的时间，用于无工作流程时自动退出。
        self._session_active_at: dict[str, float] = {}
        self._published_working: Optional[tuple[str, ...]] = None
        self._shown: dict[tuple[str, str], float] = {}
        self._last_replace_log = 0.0
        self._last_error_log = 0.0

    @property
    def stats(self) -> str:
        """诊断用：当前绑定的库、基线长度与记账的文件尺寸。"""
        with self._lock:
            name = Path(self._path).name[:8] if self._path else "-"
            return f"lib={name} snap={len(self._snapshot)} size={self._stamp[1]}"

    @property
    def is_watching(self) -> bool:
        """是否已绑定至少一个真实会话库进行轮询。"""
        with self._lock:
            return self._path is not None

    @property
    def working_ids(self) -> tuple[str, ...]:
        """当前所有仍在工作中的会话标识集合。"""
        with self._lock:
            return tuple(sorted(self._working_sessions))

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

    def _seed_known_stamps(self, primary_path: Optional[str] = None) -> None:
        """建基线时记录现有会话文件戳与行数，并纳管启动当下已有 RUNNING 步骤的并发会话。"""
        self._known_stamps.clear()
        self._known_progress.clear()
        for path in self._service.candidate_paths(12):
            path_str = str(path)
            stamp = self._read_stamp(path_str)
            if stamp is None:
                continue
            self._known_stamps[path_str] = stamp
            try:
                total, max_idx, has_running = AntigravityChatSyncService.probe_session_activity(path)
            except (sqlite3.Error, OSError, ValueError):
                continue
            self._known_progress[path_str] = (total, max_idx)
            if path_str == primary_path or not has_running:
                continue
            try:
                conv, cache = AntigravityChatSyncService.read_incremental(path)
            except (sqlite3.Error, OSError, ValueError):
                continue
            # 仅把当前存在活跃运行步骤的并发会话纳入初始工作集。
            if conv.has_running_step:
                self._peers[path_str] = _PeerSession(
                    path=path_str,
                    source_id=conv.source_id,
                    stamp=stamp,
                    cache=cache,
                    snapshot=_signature(conv.messages),
                )
                self._working_sessions.add(conv.source_id)
                self._session_active_at[conv.source_id] = self._clock()

    def watch(self, conversation: SyncedConversation) -> None:
        """绑定会话并按当前内容建基线：已有历史只作快照，同时接管已在运行中的并发会话。"""
        with self._lock:
            self._peers.clear()
            self._working_sessions.clear()
            self._session_active_at.clear()
            self._published_working = None
            self._seed_known_stamps(primary_path=str(conversation.db_path))
            self._bind(conversation, switched=False)
            if conversation.has_running_step:
                self._working_sessions.add(conversation.source_id)
                self._session_active_at[conversation.source_id] = self._clock()
            if self._working_sessions:
                self._publish_working(completed=False)

    def reset(self) -> None:
        with self._lock:
            self._path = None
            self._source_id = ""
            self._stamp = (0, 0, 0, 0)
            self._cache = None
            self._snapshot = []
            self._known_stamps.clear()
            self._known_progress.clear()
            self._peers.clear()
            self._working_sessions.clear()
            self._session_active_at.clear()
            self._published_working = None
            self._shown.clear()
            self._ticks = 0
            self._last_replace_log = 0.0
            self._last_error_log = 0.0

    def poll(self) -> None:
        """采集一次；主会话和并发工作会话按 stat 差量增量读取，无变化不触库。"""
        with self._lock:
            if self._path is None:
                return
            self._ticks += 1
            if self._ticks % self._follow_interval == 0:
                self._follow_newest()
            if self._ticks % self._scan_interval == 0:
                self._scan_other_sessions(force_probe=(self._ticks % (self._scan_interval * 3) == 0))
            self._poll_primary()
            self._poll_peers()

    def _poll_primary(self) -> None:
        if self._path is None:
            return
        stamp = self._read_stamp(self._path)
        if stamp is None:
            if self._source_id in self._working_sessions:
                self._working_sessions.discard(self._source_id)
                if not self._working_sessions:
                    self._scan_other_sessions(force_probe=True)
                self._publish_working(completed=False)
            return
        # 工作中每 4 拍、平时每 force_interval 拍兜底复核，绕过 Windows NTFS 对打开中 WAL 的 stat 缓存。
        active_primary = self._source_id in self._working_sessions
        due_recheck = (
            (active_primary and self._ticks % 4 == 0)
            or self._ticks % self._force_interval == 0
        )
        if stamp == self._stamp and not due_recheck:
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
        self._known_stamps[self._path] = stamp
        self._known_progress[self._path] = (cache.rows, cache.max_idx)
        self._cache = cache
        self._publish(conversation)

    def _scan_other_sessions(self, *, force_probe: bool = False) -> None:
        """发现并纳管建基线后新产生写入或正在执行中的其他并发工作会话。"""
        for path in self._service.candidate_paths(12):
            path_str = str(path)
            if path_str == self._path or path_str in self._peers:
                continue
            stamp = self._read_stamp(path_str)
            if stamp is None:
                continue
            had_known = path_str in self._known_stamps
            stamp_changed = (not had_known) or (self._known_stamps[path_str] != stamp)
            has_nonempty_wal = stamp[3] > 0
            if not stamp_changed and not force_probe and not has_nonempty_wal:
                continue

            # 若 stat 未变（例如 Windows NTFS 缓存打开中的 WAL 元数据），先轻量探测行数与 RUNNING 状态。
            if not stamp_changed:
                try:
                    total, max_idx, has_running = AntigravityChatSyncService.probe_session_activity(path)
                except (sqlite3.Error, OSError, ValueError):
                    continue
                prev_prog = self._known_progress.get(path_str)
                self._known_progress[path_str] = (total, max_idx)
                if not has_running and prev_prog == (total, max_idx):
                    continue

            self._known_stamps[path_str] = stamp
            try:
                conversation, cache = AntigravityChatSyncService.read_incremental(path)
            except (sqlite3.Error, OSError, ValueError):
                continue
            prev_prog = self._known_progress.get(path_str)
            self._known_progress[path_str] = (cache.rows, cache.max_idx)
            if not conversation.messages and not conversation.has_running_step:
                continue
            progressed = prev_prog is None or prev_prog != (cache.rows, cache.max_idx)
            is_working = conversation.has_running_step or (
                _is_conversation_working(conversation)
                and (
                    not had_known
                    or progressed
                    or _is_recent_message_time(
                        conversation.messages, max_age_sec=self._idle_work_timeout
                    )
                )
            )
            if is_working:
                self._peers[path_str] = _PeerSession(
                    path=path_str,
                    source_id=conversation.source_id,
                    stamp=stamp,
                    cache=cache,
                    snapshot=_signature(conversation.messages),
                )
                self._working_sessions.add(conversation.source_id)
                self._session_active_at[conversation.source_id] = self._clock()
                self._publish_working(completed=False)
            elif conversation.source_id in self._working_sessions:
                self._working_sessions.discard(conversation.source_id)
                self._session_active_at.pop(conversation.source_id, None)
                self._publish_working(completed=conversation.is_completed)

    def _is_idle_without_workflow(
        self, source_id: str, conversation: SyncedConversation, changed: bool
    ) -> bool:
        """无任何 PENDING/RUNNING 步骤且长时间无新写入时，视为取消或无工作流程并自动退出。"""
        now = self._clock()
        if conversation.has_running_step or changed:
            self._session_active_at[source_id] = now
            return False
        last_active = self._session_active_at.setdefault(source_id, now)
        return (now - last_active) >= self._idle_work_timeout

    def _poll_peers(self) -> None:
        """持续轮询所有并发工作中的后台会话，直到它们全部完成或取消空闲超时才释放工作态。"""
        if not self._peers:
            return
        for path_str, peer in list(self._peers.items()):
            stamp = self._read_stamp(path_str)
            if stamp is None:
                self._peers.pop(path_str, None)
                self._working_sessions.discard(peer.source_id)
                self._session_active_at.pop(peer.source_id, None)
                if not self._working_sessions:
                    self._scan_other_sessions(force_probe=True)
                self._publish_working(completed=False)
                continue
            # 后台工作会话每 4 拍复核一次，避免 NTFS WAL stat 缓存拖慢完成或取消检测。
            if stamp == peer.stamp and self._ticks % 4 != 0:
                continue
            try:
                conversation, cache = AntigravityChatSyncService.read_incremental(
                    Path(path_str), peer.cache
                )
            except (sqlite3.Error, OSError, ValueError):
                continue
            peer.stamp = stamp
            peer.cache = cache
            self._known_stamps[path_str] = stamp
            self._known_progress[path_str] = (cache.rows, cache.max_idx)
            previous = peer.snapshot
            current = _signature(conversation.messages)
            peer.snapshot = current
            if _is_conversation_working(conversation) and not self._is_idle_without_workflow(
                peer.source_id, conversation, changed=(current != previous)
            ):
                self._working_sessions.add(peer.source_id)
                self._publish_working(completed=False)
                continue
            self._peers.pop(path_str, None)
            self._working_sessions.discard(peer.source_id)
            self._session_active_at.pop(peer.source_id, None)
            if not self._working_sessions:
                # 即将宣告全部结束前立刻穿透 stat 复查其他会话库，防止并发会话漏扫。
                self._scan_other_sessions(force_probe=True)
            finished_ok = bool(conversation.is_completed and not self._working_sessions)
            self._publish_working(completed=finished_ok)
            # 仅正常完成且成为最后一个结束的后台会话时，补发其最终回复刷新气泡并衔接开心动作。
            if finished_ok and conversation.messages:
                newest = conversation.messages[-1]
                if self._allow(newest):
                    self._emit(
                        [self._event(newest, source_id=peer.source_id, in_progress=False)],
                        replace=False,
                    )

    def _publish_working(self, *, completed: bool) -> None:
        active = tuple(sorted(self._working_sessions))
        if active == self._published_working:
            return
        self._published_working = active
        self._state.write("work", {"active_ids": list(active), "completed": completed})

    def _bind(
        self,
        conversation: SyncedConversation,
        *,
        switched: bool,
        cache: Optional[ConversationCache] = None,
    ) -> None:
        path = conversation.db_path
        path_str = str(path)
        if switched and self._path and self._path != path_str and self._source_id in self._working_sessions:
            # 切去跟踪新活跃会话时，把仍在工作中的旧主会话移入后台并发追踪，不丢其工作态。
            self._peers[self._path] = _PeerSession(
                path=self._path,
                source_id=self._source_id,
                stamp=self._stamp,
                cache=self._cache,
                snapshot=list(self._snapshot),
            )
        reused_peer = self._peers.pop(path_str, None)
        if cache is None and reused_peer is not None:
            cache = reused_peer.cache
        if cache is None:
            # 建基线时顺手把逐步骤缓存一起建好，之后流式刷新就只解尾部。
            try:
                conversation, cache = AntigravityChatSyncService.read_incremental(path)
            except (sqlite3.Error, OSError, ValueError):
                cache = None
        self._path = path_str
        self._source_id = conversation.source_id
        self._cache = cache
        self._stamp = self._read_stamp(self._path) or (
            conversation.mtime_ns,
            conversation.size,
            0,
            0,
        )
        self._known_stamps[self._path] = self._stamp
        if cache is not None:
            self._known_progress[self._path] = (cache.rows, cache.max_idx)
        self._snapshot = _signature(conversation.messages)
        if switched:
            if _is_conversation_working(conversation):
                self._working_sessions.add(conversation.source_id)
                self._session_active_at[conversation.source_id] = self._clock()
                self._publish_working(completed=False)
            elif conversation.source_id in self._working_sessions:
                self._working_sessions.discard(conversation.source_id)
                self._session_active_at.pop(conversation.source_id, None)
                if not self._working_sessions:
                    self._scan_other_sessions(force_probe=True)
                self._publish_working(
                    completed=bool(conversation.is_completed and not self._working_sessions)
                )
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
        source_id = conversation.source_id
        was_in_working = source_id in self._working_sessions
        current = _signature(messages)
        previous = self._snapshot
        changed = current != previous
        raw_working = _is_conversation_working(conversation)
        timed_out_idle = (
            raw_working
            and was_in_working
            and self._is_idle_without_workflow(source_id, conversation, changed=changed)
        )
        working = raw_working and not timed_out_idle
        stopped = (not working) and (not conversation.is_completed)

        # 步骤状态从活跃变为完成/取消或空闲无流程超时时，先同步工作状态再做文本去重。
        if working:
            if changed or conversation.has_running_step or was_in_working:
                self._working_sessions.add(source_id)
                if changed or conversation.has_running_step or source_id not in self._session_active_at:
                    self._session_active_at[source_id] = self._clock()
                self._publish_working(completed=False)
        elif was_in_working or changed:
            self._working_sessions.discard(source_id)
            self._session_active_at.pop(source_id, None)
            if not self._working_sessions:
                # 即将宣告全部结束前先穿透 stat 复查其他会话库，确保刚启动或 WAL 未刷新 stat 的并发会话已被纳入。
                self._scan_other_sessions(force_probe=True)
                self._poll_peers()
            if was_in_working or self._working_sessions:
                finished_ok = bool(conversation.is_completed and not self._working_sessions)
                self._publish_working(completed=finished_ok)

        if not messages or not changed:
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
                last_idx = len(fresh) - 1
                self._emit(
                    [
                        self._event(
                            message,
                            source_id=source_id,
                            in_progress=working and idx == last_idx,
                            stopped=stopped and idx == last_idx,
                        )
                        for idx, message in enumerate(fresh)
                    ],
                    replace=False,
                )
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
                self._emit(
                    [
                        self._event(
                            newest,
                            replace=True,
                            source_id=source_id,
                            in_progress=working,
                            stopped=stopped,
                        )
                    ],
                    replace=True,
                )
            return

        # 结构对不上（会话被重写或切换）：重建基线，只补最后一条，不重放历史。
        AppLogger.info("ChatSync", f"会话结构变化，重建基线（{len(previous)}→{len(current)}）")
        self._snapshot = current
        newest = messages[-1]
        if self._allow(newest):
            self._emit(
                [
                    self._event(
                        newest,
                        source_id=source_id,
                        in_progress=working,
                        stopped=stopped,
                    )
                ],
                replace=False,
            )

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
    def _event(
        message: SyncedMessage,
        *,
        replace: bool = False,
        source_id: str = "",
        in_progress: bool = False,
        stopped: bool = False,
    ) -> SyncEvent:
        kind, text = message.kind, message.text
        if kind == "tool":
            reason = failure_reason(text)
            if reason:
                kind, text = "error", f"{text}\n原因：{reason}"
        return SyncEvent(
            role=message.role,
            text=text,
            kind=kind,
            time=message.time,
            replace=replace,
            source_id=source_id,
            in_progress=in_progress,
            stopped=stopped,
        )

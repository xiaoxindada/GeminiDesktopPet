"""Antigravity 会话库提取、增量解析与同步采集器验证。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from services.antigravity_chat_collector import AntigravityChatCollector
from services.antigravity_chat_sync_service import AntigravityChatSyncService
from services.chat_sync_state import ChatSyncState


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _int_field(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _step_time(seconds: int) -> bytes:
    return _bytes_field(5, _bytes_field(1, _int_field(1, seconds)))


def _user(text: str) -> bytes:
    return _bytes_field(19, _bytes_field(2, text.encode())) + _step_time(1794541874)


def _assistant(text: str, thinking: str = "") -> bytes:
    payload = b""
    if thinking:
        payload += _bytes_field(20, _bytes_field(3, thinking.encode()))
    return payload + _bytes_field(20, _bytes_field(1, text.encode())) + _step_time(1794541885)


def _tool(command: str, output: str, summary: str = "查看状态") -> tuple[bytes, bytes]:
    metadata = _bytes_field(
        4,
        _bytes_field(2, command.encode())
        + _bytes_field(3, json.dumps({"toolSummary": summary}, ensure_ascii=False).encode()),
    )
    payload = _bytes_field(140, _bytes_field(2, _bytes_field(1, output.encode())))
    return metadata, payload


def _create_db(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "create table steps (idx integer, step_type integer, metadata blob, step_payload blob)"
        )
        connection.executemany("insert into steps values (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


class TestAntigravityChatSync(unittest.TestCase):
    def test_extracts_messages_thinking_and_tool_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.db"
            user_payload = _user("示例问题")
            answer_payload = _assistant("示例回答", "思考示例")
            tool_metadata, tool_payload = _tool(
                "run_command", "Command exited with code 0. Command output: ok"
            )
            _create_db(
                path,
                [
                    (0, 14, b"", user_payload),
                    (1, 15, b"", answer_payload),
                    (2, 132, tool_metadata, tool_payload),
                    (3, 15, b"", b""),
                ],
            )

            conversation = AntigravityChatSyncService.read_conversation(path)
            self.assertEqual(
                [message.kind for message in conversation.messages],
                ["message", "thinking", "message", "tool"],
            )
            self.assertEqual(
                [message.role for message in conversation.messages[:3]],
                ["user", "system", "assistant"],
            )
            self.assertEqual(conversation.messages[1].text, "思考示例")
            self.assertEqual(conversation.messages[2].text, "示例回答")
            tool_text = conversation.messages[3].text
            self.assertIn("run_command", tool_text)
            self.assertIn("查看状态", tool_text)
            self.assertIn("退出码 0", tool_text)
            self.assertTrue(conversation.messages[0].time.startswith("2026-"))
            self.assertEqual(conversation.title, "示例问题")

    def test_long_content_and_thinking_suffix_are_preserved(self) -> None:
        from services.antigravity_chat_sync_service import _decode_step

        text = ("完整长输出，保留换行。\n" * 700) + "末尾标记"
        thinking = text + "思维后缀"
        payload = _bytes_field(
            20, _bytes_field(3, thinking.encode()) + _bytes_field(1, text.encode())
        )
        seen = set()
        messages, _ = _decode_step(15, b"", payload, seen)
        self.assertEqual([m.text for m in messages], [thinking, text])
        extended = _bytes_field(20, _bytes_field(3, (thinking + "续写").encode()))
        messages, _ = _decode_step(15, b"", extended, seen)
        self.assertEqual(messages[0].text, thinking + "续写")
        user = _bytes_field(19, _bytes_field(2, text.encode()))
        messages, _ = _decode_step(14, b"", user, set())
        self.assertEqual(messages[0].text, text)

    def test_repeated_thinking_text_is_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repeat.db"
            step = _assistant("回答", "重复思考")
            _create_db(path, [(0, 15, b"", step), (1, 15, b"", step)])

            conversation = AntigravityChatSyncService.read_conversation(path)
            thinking = [message for message in conversation.messages if message.kind == "thinking"]
            self.assertEqual(len(thinking), 1)
            self.assertEqual(thinking[0].text, "重复思考")

    def test_latest_conversation_skips_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _create_db(directory / "empty.db", [])
            _create_db(directory / "full.db", [(0, 14, b"", _user("有内容的会话"))])

            service = AntigravityChatSyncService((directory,))
            conversation = service.latest_conversation()
            self.assertIsNotNone(conversation)
            assert conversation is not None
            self.assertEqual(conversation.source_id, "full")
            self.assertEqual(conversation.messages[0].text, "有内容的会话")


class TestIncrementalDecode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "s1.db"
        _create_db(
            self.path,
            [
                (0, 14, b"", _user("问题一")),
                (1, 15, b"", _assistant("回答一", "想一想")),
                (2, 14, b"", _user("问题二")),
                (3, 15, b"", _assistant("回答二")),
            ],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _texts(self, messages) -> list[str]:
        return [f"{m.role}/{m.kind}/{m.text}" for m in messages]

    def _write(self, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def test_first_read_matches_full_decode(self) -> None:
        incremental, cache = AntigravityChatSyncService.read_incremental(self.path)
        full = AntigravityChatSyncService.read_conversation(self.path)
        self.assertEqual(self._texts(incremental.messages), self._texts(full.messages))
        self.assertEqual(cache.rows, 4)
        # 4 步 = 两条提问 + 一条带思考的回复（思考单算一条）+ 一条回复。
        self.assertEqual(len(incremental.messages), 5)

    def test_rewritten_tail_matches_full_decode(self) -> None:
        _first, cache = AntigravityChatSyncService.read_incremental(self.path)
        self._write(
            "update steps set step_payload = ? where idx = 3",
            (_assistant("回答二，写完了"),),
        )
        incremental, cache = AntigravityChatSyncService.read_incremental(self.path, cache)
        full = AntigravityChatSyncService.read_conversation(self.path)
        self.assertEqual(self._texts(incremental.messages), self._texts(full.messages))
        self.assertEqual(incremental.messages[-1].text, "回答二，写完了")

    def test_appended_step_reuses_cached_prefix(self) -> None:
        _first, cache = AntigravityChatSyncService.read_incremental(self.path)
        self._write("insert into steps values (4, 14, ?, ?)", (b"", _user("问题三")))
        # 重复的思考文本应该继续被去重，说明前缀的去重集合被正确重建。
        self._write("insert into steps values (5, 15, ?, ?)", (b"", _assistant("回答三", "想一想")))
        incremental, cache = AntigravityChatSyncService.read_incremental(self.path, cache)
        full = AntigravityChatSyncService.read_conversation(self.path)
        self.assertEqual(self._texts(incremental.messages), self._texts(full.messages))
        self.assertEqual(incremental.messages[-1].text, "回答三")
        self.assertEqual(
            [m.text for m in incremental.messages].count("想一想"), 1, "思考文本不应重复出现"
        )

    def test_removed_row_falls_back_to_full_decode(self) -> None:
        _first, cache = AntigravityChatSyncService.read_incremental(self.path)
        self._write("delete from steps where idx = 1")
        incremental, cache = AntigravityChatSyncService.read_incremental(self.path, cache)
        full = AntigravityChatSyncService.read_conversation(self.path)
        self.assertEqual(self._texts(incremental.messages), self._texts(full.messages))
        self.assertEqual(cache.rows, 3)

    def test_corrupt_payload_is_skipped(self) -> None:
        self._write("update steps set step_payload = ? where idx = 3", (b"\x00\xff",))
        incremental, _cache = AntigravityChatSyncService.read_incremental(self.path)
        self.assertEqual([m.text for m in incremental.messages], ["问题一", "想一想", "回答一", "问题二"])

    def test_empty_database_yields_empty_conversation(self) -> None:
        empty = Path(self._tmp.name) / "empty.db"
        _create_db(empty, [])
        conversation, cache = AntigravityChatSyncService.read_incremental(empty)
        self.assertEqual(conversation.messages, [])
        self.assertEqual(cache.rows, 0)


class TestAntigravityChatCollector(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.path = self.directory / "s1.db"
        _create_db(self.path, [(0, 14, b"", _user("问题一"))])
        self.service = AntigravityChatSyncService((self.directory,))
        self.state = ChatSyncState()
        self.collector = AntigravityChatCollector(self.service, self.state, clock=lambda: 0.0)
        conversation = self.service.latest_conversation()
        assert conversation is not None
        self.collector.watch(conversation)
        self.baseline = self.state.counters()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()
        os.utime(self.path, None)

    def _changes(self) -> dict[str, object]:
        self.collector.poll()
        return dict(self.state.take_since(self.baseline))

    def test_appended_messages_are_emitted_together(self) -> None:
        self._write("insert into steps values (1, 15, ?, ?)", (b"", _assistant("回答一")))
        self._write("insert into steps values (2, 14, ?, ?)", (b"", _user("问题二")))
        changes = self._changes()
        items = changes["events"]["items"]
        self.assertEqual([event.text for event in items], ["回答一", "问题二"])
        self.assertFalse(changes["events"]["replace"])

    def test_growing_answer_is_marked_as_replacement(self) -> None:
        self._write("insert into steps values (1, 15, ?, ?)", (b"", _assistant("回答")))
        self._changes()
        self._write("update steps set step_payload = ? where idx = 1", (_assistant("回答更完整了"),))
        changes = self._changes()
        self.assertTrue(changes["events"]["replace"])
        self.assertEqual(changes["events"]["items"][-1].text, "回答更完整了")

    def test_finished_answer_and_next_question_are_both_emitted(self) -> None:
        self._write("insert into steps values (1, 15, ?, ?)", (b"", _assistant("回答")))
        self._changes()
        self._write("update steps set step_payload = ? where idx = 1", (_assistant("回答完成"),))
        self._write("insert into steps values (2, 14, ?, ?)", (b"", _user("问题二")))
        items = self._changes()["events"]["items"]
        self.assertEqual([event.text for event in items], ["回答完成", "问题二"])

    def test_identical_tool_notice_is_suppressed_within_cooldown(self) -> None:
        metadata, payload = _tool("run_command", "Command exited with code 0. Command output: ok")
        self._write("insert into steps values (1, 132, ?, ?)", (metadata, payload))
        first = self._changes()
        self.assertEqual(first["events"]["items"][-1].kind, "tool")
        self._write("insert into steps values (2, 132, ?, ?)", (metadata, payload))
        self.assertNotIn("events", self._changes())

    def test_failed_tool_is_labelled_with_reason(self) -> None:
        metadata, payload = _tool("run_command", "Command exited with code 2. Command output: boom")
        self._write("insert into steps values (1, 132, ?, ?)", (metadata, payload))
        event = self._changes()["events"]["items"][-1]
        self.assertEqual(event.kind, "error")
        self.assertIn("命令返回码 2", event.text)

    def test_permission_failure_is_reported_as_such(self) -> None:
        metadata, payload = _tool("run_command", "Command exited with code 1. Command output: denied")
        self._write("insert into steps values (1, 132, ?, ?)", (metadata, payload))
        event = self._changes()["events"]["items"][-1]
        self.assertEqual(event.kind, "error")
        self.assertIn("权限不足或访问被拒绝", event.text)

    def test_switches_to_newer_conversation(self) -> None:
        newer = self.directory / "s2.db"
        _create_db(newer, [(0, 14, b"", _user("新的会话"))])
        stamp = time.time() + 60
        os.utime(newer, (stamp, stamp))
        status = {}
        for _ in range(self.collector._follow_interval):
            self.collector.poll()
            status.update(
                {name: data for name, data in self.state.take_since(self.baseline) if name == "status"}
            )
        self.assertTrue(status["status"]["switched"])
        self.assertEqual(status["status"]["title"], "新的会话")

    def test_concurrent_working_sessions_wait_until_all_finish(self) -> None:
        # 历史未完成会话（基线前已存在且无新写入）不应被误判为正在工作。
        stale = self.directory / "stale.db"
        _create_db(stale, [(0, 14, b"", _user("昨天的中断会话"))])
        old_ts = time.time() - 3600
        os.utime(stale, (old_ts, old_ts))

        collector = AntigravityChatCollector(
            self.service,
            self.state,
            follow_interval=2,
            scan_interval=1,
            clock=lambda: 0.0,
        )
        conversation = AntigravityChatSyncService.read_conversation(self.path)
        collector.watch(conversation)
        baseline = self.state.counters()

        # 会话 s1 开始工作（思考中）
        self._write("insert into steps values (1, 15, ?, ?)", (b"", _assistant("", "s1 思考中")))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(changes["work"]["active_ids"], ["s1"])

        # 并发开启第二个工作会话 s2（新建会话库）
        s2 = self.directory / "s2.db"
        _create_db(s2, [(0, 14, b"", _user("s2 新任务"))])
        stamp2 = time.time() + 10
        os.utime(s2, (stamp2, stamp2))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(changes["work"]["active_ids"], ["s1", "s2"])
        self.assertNotIn("stale", changes["work"]["active_ids"])

        # s2 先完成回复，s1 仍在后台工作：工作集合仍保留 s1，不提前结束
        conn = sqlite3.connect(s2)
        try:
            conn.execute("insert into steps values (1, 15, ?, ?)", (b"", _assistant("s2 已完成")))
            conn.commit()
        finally:
            conn.close()
        stamp3 = time.time() + 20
        os.utime(s2, (stamp3, stamp3))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(changes["work"]["active_ids"], ["s1"])

        # 最后 s1 也完成回复：工作集合清空，并补发 s1 完成消息刷新气泡
        self._write("insert into steps values (2, 15, ?, ?)", (b"", _assistant("s1 也完成了")))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(changes["work"]["active_ids"], [])
        self.assertTrue(changes["work"]["completed"])
        self.assertEqual(changes["events"]["items"][-1].text, "s1 也完成了")
        self.assertEqual(changes["events"]["items"][-1].source_id, "s1")

    def test_streaming_assistant_status_running_keeps_work_active_when_other_session_finishes(self) -> None:
        # 复现真实场景：会话 A 的助手回复处于 status=2（流式生成中），会话 B 在此时完成（status=3），
        # 必须识别会话 A 仍在生成中、不提前结束工作态，直到 A 的 status 变为 3（DONE）才宣告全部完成。
        def _create_status_db(db_path: Path, rows: list[tuple]) -> None:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "create table steps (idx integer, step_type integer, status integer, metadata blob, step_payload blob)"
                )
                conn.executemany("insert into steps values (?, ?, ?, ?, ?)", rows)
                conn.commit()
            finally:
                conn.close()

        a_path = self.directory / "conv_stream.db"
        b_path = self.directory / "conv_fast.db"
        _create_status_db(b_path, [(0, 14, 3, b"", _user("快速任务")), (1, 15, 3, b"", _assistant("历史回复"))])
        # 会话 A 在建基线时已存在流式生成中的步骤（status=2，且已输出部分回答文本）
        _create_status_db(
            a_path,
            [
                (0, 14, 3, b"", _user("长思考任务")),
                (1, 15, 2, b"", _assistant("正在流式输出前半段", "长思考")),
            ],
        )
        ts = time.time() + 10
        os.utime(b_path, (ts + 5, ts + 5))
        os.utime(a_path, (ts, ts))

        collector = AntigravityChatCollector(
            self.service,
            self.state,
            follow_interval=25,
            scan_interval=10,  # 故意设大，验证主会话完成前会立刻主动复查并发会话
            clock=lambda: 0.0,
        )
        collector.watch(AntigravityChatSyncService.read_conversation(b_path))
        self.assertEqual(collector.working_ids, ("conv_stream",))
        baseline = self.state.counters()

        # 会话 B 开始新一轮并立刻完成（status=3）
        conn = sqlite3.connect(b_path)
        try:
            conn.execute("insert into steps values (2, 14, 3, ?, ?)", (b"", _user("快任务二")))
            conn.execute("insert into steps values (3, 15, 3, ?, ?)", (b"", _assistant("快任务二完成")))
            conn.commit()
        finally:
            conn.close()
        os.utime(b_path, (ts + 10, ts + 10))
        collector.poll()
        self.assertEqual(collector.working_ids, ("conv_stream",))

        # 会话 A 更新流式文案但仍是 status=2：继续保持工作态
        conn = sqlite3.connect(a_path)
        try:
            conn.execute(
                "update steps set step_payload = ? where idx = 1",
                (_assistant("长任务最终完整回答", "长思考"),),
            )
            conn.commit()
        finally:
            conn.close()
        os.utime(a_path, (ts + 15, ts + 15))
        collector.poll()
        self.assertEqual(collector.working_ids, ("conv_stream",))

        # 会话 A 最终把 status 从 2 改为 3（文案不变）：此时才宣告全部完成并补发最终回复
        conn = sqlite3.connect(a_path)
        try:
            conn.execute("update steps set status = 3 where idx = 1")
            conn.commit()
        finally:
            conn.close()
        os.utime(a_path, (ts + 20, ts + 20))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(collector.working_ids, ())
        self.assertEqual(changes["work"]["active_ids"], [])
        self.assertTrue(changes["work"]["completed"])
        self.assertEqual(changes["events"]["items"][-1].text, "长任务最终完整回答")
        self.assertFalse(changes["events"]["items"][-1].in_progress)

        # 验证 Windows NTFS 对打开中 WAL 缓存 stat（mtime/size 未变化）时，
        # 主会话完成前的穿透探测仍能发现并发会话并阻止提前退出。
        prev_stat = a_path.stat()
        conn = sqlite3.connect(a_path)
        try:
            conn.execute("insert into steps values (2, 14, 3, ?, ?)", (b"", _user("第三轮并发")))
            conn.execute("insert into steps values (3, 15, 2, ?, ?)", (b"", _assistant("流式输出中", "思考")))
            conn.commit()
        finally:
            conn.close()
        # 故意把 mtime 还原，模拟 NTFS stat 缓存未刷新
        os.utime(a_path, ns=(prev_stat.st_atime_ns, prev_stat.st_mtime_ns))
        collector._known_stamps[str(a_path)] = collector._read_stamp(str(a_path)) or (0, 0, 0, 0)

        conn = sqlite3.connect(b_path)
        try:
            conn.execute("insert into steps values (4, 14, 3, ?, ?)", (b"", _user("快任务三")))
            conn.execute("insert into steps values (5, 15, 3, ?, ?)", (b"", _assistant("快任务三完成")))
            conn.commit()
        finally:
            conn.close()
        os.utime(b_path, (ts + 30, ts + 30))
        collector.poll()
        self.assertEqual(collector.working_ids, ("conv_stream",))

    def test_canceled_or_idle_without_running_workflow_releases_work_state(self) -> None:
        def _create_status_db(db_path: Path, rows: list[tuple]) -> None:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "create table steps (idx integer, step_type integer, status integer, metadata blob, step_payload blob)"
                )
                conn.executemany("insert into steps values (?, ?, ?, ?, ?)", rows)
                conn.commit()
            finally:
                conn.close()

        # 1. 已完成回复后追加的 step_type=101 元信息步骤不应被误判为工作中。
        post_meta_db = self.directory / "post_meta.db"
        _create_status_db(
            post_meta_db,
            [
                (0, 14, 3, b"", _user("已完成提问")),
                (1, 15, 3, b"", _assistant("已经答复")),
                (2, 101, 3, b"", b""),
            ],
        )
        post_conv = AntigravityChatSyncService.read_conversation(post_meta_db)
        self.assertFalse(post_conv.is_working)
        self.assertTrue(post_conv.is_completed)

        # 1.5 提问后出现的 step_type=17 预思考步骤以及 status=9（流式生成中）必须保持工作中，不得提前结束。
        thinking_db = self.directory / "thinking_gen.db"
        _create_status_db(
            thinking_db,
            [
                (0, 14, 3, b"", _user("深度思考提问")),
                (1, 17, 3, b"", b""),
            ],
        )
        pre_conv = AntigravityChatSyncService.read_conversation(thinking_db)
        self.assertTrue(pre_conv.is_working)
        self.assertFalse(pre_conv.is_completed)

        conn = sqlite3.connect(thinking_db)
        try:
            conn.execute(
                "insert into steps values (2, 15, 9, ?, ?)",
                (b"", _assistant("正在流式输出内容...", "长思考中")),
            )
            conn.commit()
        finally:
            conn.close()
        gen_conv = AntigravityChatSyncService.read_conversation(thinking_db)
        self.assertTrue(gen_conv.has_running_step)
        self.assertTrue(gen_conv.is_working)
        self.assertFalse(gen_conv.is_completed)
        self.assertTrue(AntigravityChatSyncService.probe_session_activity(thinking_db)[2])

        # 流式生成完成后（status=3）才视为完成，避免影响后续独立取消用例。
        conn = sqlite3.connect(thinking_db)
        try:
            conn.execute("update steps set status = 3 where idx = 2")
            conn.commit()
        finally:
            conn.close()
        done_conv = AntigravityChatSyncService.read_conversation(thinking_db)
        self.assertFalse(done_conv.has_running_step)
        self.assertFalse(done_conv.is_working)
        self.assertTrue(done_conv.is_completed)

        # 2. 用户显式取消执行（status=6）：立即退出工作态，completed=False（不切开心动作）。
        now = [100.0]
        cancel_db = self.directory / "cancel_case.db"
        _create_status_db(cancel_db, [(0, 14, 3, b"", _user("第一轮"))])
        os.utime(cancel_db, (time.time() + 40, time.time() + 40))
        collector = AntigravityChatCollector(
            self.service,
            self.state,
            follow_interval=25,
            scan_interval=5,
            idle_work_timeout=8.0,
            clock=lambda: now[0],
        )
        collector.watch(AntigravityChatSyncService.read_conversation(cancel_db))
        baseline = self.state.counters()

        meta, pl = _tool("run_command", "Command exited with code 0. Command output: running")
        conn = sqlite3.connect(cancel_db)
        try:
            conn.execute("insert into steps values (1, 132, 2, ?, ?)", (meta, pl))
            conn.commit()
        finally:
            conn.close()
        os.utime(cancel_db, (time.time() + 45, time.time() + 45))
        collector.poll()
        self.assertEqual(collector.working_ids, ("cancel_case",))

        # 用户取消工具执行：status 变为 6（CANCELED）
        conn = sqlite3.connect(cancel_db)
        try:
            conn.execute("update steps set status = 6 where idx = 1")
            conn.commit()
        finally:
            conn.close()
        os.utime(cancel_db, (time.time() + 50, time.time() + 50))
        collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(collector.working_ids, ())
        self.assertEqual(changes["work"]["active_ids"], [])
        self.assertFalse(changes["work"]["completed"])

        # 3. 取消后未写 status=6、仅剩 status=3 且无 RUNNING 步骤：空闲超时自动退出工作态。
        conn = sqlite3.connect(cancel_db)
        try:
            conn.execute("insert into steps values (2, 14, 3, ?, ?)", (b"", _user("被中断的新提问")))
            conn.commit()
        finally:
            conn.close()
        os.utime(cancel_db, (time.time() + 55, time.time() + 55))
        collector.poll()
        self.assertEqual(collector.working_ids, ("cancel_case",))
        self.state.take_since(baseline)

        # 未到超时前仍保留短暂衔接态，达到 8 秒无运行中工作流后每 4 拍复核自动释放。
        now[0] += 9.0
        for _ in range(4):
            collector.poll()
        changes = dict(self.state.take_since(baseline))
        self.assertEqual(collector.working_ids, ())
        self.assertEqual(changes["work"]["active_ids"], [])
        self.assertFalse(changes["work"]["completed"])


class TestSyncBubbleChatIsolation(unittest.TestCase):
    def test_long_sync_bubble_does_not_switch_to_chat_mode_after_closing_chat(self) -> None:
        from unittest.mock import Mock

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        from models.pet_settings import PetSettings
        from services.antigravity_chat_collector import SyncEvent
        from services.gemini_chat_service import GeminiChatService
        from services.sound_service import SoundService
        from services.sprite_manager import SpriteManager
        from services.system_monitor_service import SystemMonitorService
        from views.pet_window import PetWindow

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            settings = PetSettings(
                auto_walk=False, chat_sync_enabled=False, show_speech_bubble=True
            )
            service = Mock(settings_path=Path(tmp) / "settings.json")
            pet = PetWindow(
                settings,
                service,
                SpriteManager(),
                SoundService(),
                SystemMonitorService(),
                GeminiChatService(),
            )
            try:
                pet.show()
                pet._frame_timer.stop()
                pet._ag_mirror_active = True

                # 打开再关闭聊天模式后，非聊天状态应保持普通气泡，不切回聊天消息卡片区。
                pet.open_chat_dialog()
                app.processEvents()
                pet._close_bubble_click()
                app.processEvents()

                pet._show_sync_event(SyncEvent("user", "示例提问", "message"))
                app.processEvents()
                self.assertFalse(pet._is_chat_open)
                self.assertFalse(pet._chat_messages_panel.isVisible())
                self.assertTrue(pet._bubble_text_lbl.isVisible())

                long_context = (
                    "Long path: https://example.com/" + "very_long_segment_" * 12 + "\n\n"
                    + "这是一段较长的同步思维上下文，用于验证普通气泡保持紧凑换行展示。\n" * 60
                )
                pet._show_sync_event(SyncEvent("system", long_context, "thinking"))
                app.processEvents()
                self.assertFalse(pet._is_chat_open)
                self.assertFalse(pet._chat_messages_panel.isVisible())
                self.assertTrue(pet._bubble_text_lbl.isVisible())
                self.assertEqual(pet._bubble_scroll.horizontalScrollBar().maximum(), 0)

                pet._show_sync_event(
                    SyncEvent("system", long_context + "续写结尾", "thinking", replace=True)
                )
                app.processEvents()
                self.assertFalse(pet._chat_messages_panel.isVisible())
                self.assertTrue(pet._bubble_text_lbl.isVisible())
                self.assertTrue(pet._bubble_text_lbl.text().endswith("续写结尾"))

                # 点击固定思维气泡后收到其他同步事件，也不应切出聊天卡片面板。
                QTest.mouseClick(pet._bubble_text_lbl, Qt.MouseButton.LeftButton)
                app.processEvents()
                pet._show_sync_event(SyncEvent("assistant", "普通回复", "message"))
                app.processEvents()
                self.assertFalse(pet._chat_messages_panel.isVisible())
                self.assertTrue(pet._bubble_text_lbl.isVisible())

                # 重新进入聊天模式时，应能看到关闭期间累计记录的全部实时同步事件。
                pet.open_chat_dialog()
                app.processEvents()
                self.assertTrue(pet._is_chat_open)
                self.assertTrue(pet._ag_viewing_sync)
                self.assertTrue(pet._chat_messages_panel.isVisible())
                self.assertEqual(len(pet._ag_sync_events), 3)
                self.assertEqual(len(pet._ag_sync_labels), 3)

                # 再次关闭聊天并停止同步时，不应把普通气泡切回聊天面板或覆盖气泡内容。
                pet._close_bubble_click()
                app.processEvents()
                pet.show_bubble("正常提示气泡", 3.0, force=True)
                app.processEvents()
                pet.stop_antigravity_sync(silent=True)
                app.processEvents()
                self.assertFalse(pet._is_chat_open)
                self.assertFalse(pet._chat_messages_panel.isVisible())
                self.assertTrue(pet._bubble_text_lbl.isVisible())
                self.assertEqual(pet._bubble_text_lbl.text(), "正常提示气泡")
            finally:
                pet.close()

    def test_sync_poses_fixed_per_turn_completion_standard_mode_and_isolation(self) -> None:
        from unittest.mock import Mock

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        from models.pet_settings import PetSettings, PetStyleMode
        from models.pet_state import PetState
        from services.antigravity_chat_collector import SyncEvent
        from services.gemini_chat_service import GeminiChatService
        from services.sound_service import SoundService
        from services.sprite_manager import SpriteManager
        from services.system_monitor_service import SystemMonitorService
        from views.pet_window import (
            PetWindow,
            _SYNC_BUSY_POSES,
            _SYNC_DONE_POSE,
            _SYNC_REPLACE_INTERVAL,
        )

        app = QApplication.instance() or QApplication([])
        sprites = SpriteManager()
        sprites.preload_all()
        with tempfile.TemporaryDirectory() as tmp:
            settings = PetSettings(
                auto_walk=False, chat_sync_enabled=False, show_speech_bubble=True
            )
            service = Mock(settings_path=Path(tmp) / "settings.json")
            pet = PetWindow(
                settings,
                service,
                sprites,
                SoundService(),
                SystemMonitorService(),
                GeminiChatService(),
            )
            try:
                pet.show()
                pet._frame_timer.stop()
                pet._ag_mirror_active = True
                # 清除启动问候动作，验证每轮新对话随机选定并固定前 4 个动作之一。
                pet._override_pose = None
                pet._override_is_autonomous = False
                pet._override_is_sync = False

                self.assertEqual(
                    _SYNC_BUSY_POSES,
                    (
                        PetState.BUSY_LEFT,
                        PetState.BUSY_RIGHT,
                        PetState.SWAMPED_LEFT,
                        PetState.SWAMPED_RIGHT,
                    ),
                )

                # 1. 新一轮对话开始：随机选定前 4 个动作之一，在思考/工具/异常等未完成阶段保持固定不变。
                pet._show_sync_event(SyncEvent("user", "第一轮任务", "message"))
                app.processEvents()
                turn1_pose = pet._override_pose
                self.assertIn(turn1_pose, _SYNC_BUSY_POSES)
                self.assertTrue(pet._override_is_sync)
                self.assertTrue(pet._sync_busy_active)
                self.assertEqual(pet._bubble_text_lbl.text(), "你：第一轮任务")

                # 即使新一轮用户气泡先 3 秒超时关闭，未完成前本轮动作依然保持固定。
                pet._on_bubble_timeout()
                app.processEvents()
                self.assertEqual(pet._override_pose, turn1_pose)
                self.assertTrue(pet._sync_busy_active)

                pet._show_sync_event(SyncEvent("system", "正在思考方案\n" * 60, "thinking"))
                app.processEvents()
                self.assertEqual(pet._override_pose, turn1_pose)
                self.assertTrue(pet._bubble_text_lbl.text().startswith("💭 正在思考方案"))

                # 同步进行中普通非强制气泡不得覆盖实时消息，且同步期间不触发无操作随机台词。
                pet.show_bubble("普通闲聊消息", 3.0, force=False)
                self.assertTrue(pet._bubble_text_lbl.text().startswith("💭 正在思考方案"))
                self.assertFalse(pet._can_trigger_idle_speech())

                # 工具调用与中间工具异常均属未完成阶段，动作保持与本轮一致不重新随机。
                pet._show_sync_event(SyncEvent("system", "run_command · 执行检查", "tool"))
                pet._frame_timer_tick()
                self.assertEqual(pet._override_pose, turn1_pose)
                pet._show_sync_event(SyncEvent("system", "run_command · 失败重试", "error"))
                self.assertEqual(pet._override_pose, turn1_pose)
                self.assertTrue(pet._sync_busy_active)

                # 切换到标准立绘版：保持同一固定忙碌动作并加载标准版对应贴图。
                pet._settings.style_mode = PetStyleMode.STANDARD
                pet._refresh_sprite_visual()
                self.assertEqual(pet._override_pose, turn1_pose)
                self.assertIs(pet._active_static_pixmap, sprites.get(turn1_pose, PetStyleMode.STANDARD))

                # 点击固定思维气泡并滚动到底部后，完成回复仍应切到开心（hum）动作并刷新气泡到顶部。
                pet._show_sync_event(SyncEvent("system", "最终整理思路\n" * 60, "thinking"))
                app.processEvents()
                bar = pet._bubble_scroll.verticalScrollBar()
                if bar.maximum() > 0:
                    bar.setValue(bar.maximum())
                QTest.mouseClick(pet._bubble_text_lbl, Qt.MouseButton.LeftButton)
                app.processEvents()
                self.assertTrue(pet._sync_thinking_pinned)

                pet._show_sync_event(SyncEvent("assistant", "任务已完成啦！\n" * 50, "message"))
                app.processEvents()
                self.assertEqual(pet._override_pose, _SYNC_DONE_POSE)
                self.assertIs(pet._active_static_pixmap, sprites.get(_SYNC_DONE_POSE, PetStyleMode.STANDARD))
                self.assertFalse(pet._sync_busy_active)
                self.assertIsNone(pet._sync_turn_pose)
                self.assertFalse(pet._sync_thinking_pinned)
                self.assertTrue(pet._bubble_text_lbl.text().startswith("任务已完成啦！"))
                self.assertEqual(bar.value(), bar.minimum())
                self.assertTrue(pet._bubble_timer.isActive())

                # 2. 节流窗口内的末次流式续写不应丢失，下一拍补刷后气泡应显示最终文本。
                pet._ag_last_replace = time.monotonic()
                pet._ag_sync_state.write(
                    "events",
                    {
                        "items": [SyncEvent("assistant", "最终完整回复✦", "message", replace=True)],
                        "replace": True,
                    },
                )
                pet._consume_antigravity_changes()
                self.assertIsNotNone(pet._ag_pending_replace)
                pet._ag_last_replace -= _SYNC_REPLACE_INTERVAL + 0.05
                pet._play_antigravity_queue()
                app.processEvents()
                self.assertIsNone(pet._ag_pending_replace)
                self.assertEqual(pet._bubble_text_lbl.text(), "最终完整回复✦")
                self.assertEqual(pet._override_pose, _SYNC_DONE_POSE)

                # 3. 下一轮新对话在标准版下同样从前 4 个动作中重新随机选定（避开上一轮动作）。
                pet._show_sync_event(SyncEvent("user", "第二轮任务", "message"))
                app.processEvents()
                turn2_pose = pet._override_pose
                self.assertIn(turn2_pose, _SYNC_BUSY_POSES)
                self.assertNotEqual(turn2_pose, turn1_pose)
                self.assertIs(pet._active_static_pixmap, sprites.get(turn2_pose, PetStyleMode.STANDARD))

                # 3.5 工作期间屏蔽除查看额度外的全部交互动作，查看额度放行并在额度动作结束后回到固定工作动作。
                from datetime import datetime, timedelta, timezone
                from types import SimpleNamespace
                from PySide6.QtCore import QEvent, QPointF
                from PySide6.QtGui import QMouseEvent

                self.assertTrue(pet._is_sync_working_locked())
                pat_before = pet._settings.total_pats
                pet.trigger_pat()
                pet._handle_body_poke(
                    time.monotonic(), QPointF(100.0, 200.0), pet._get_local_sprite_rect(), is_double_click=False
                )
                pet.preview_state(PetState.STRETCH)
                pet.show_and_greet("测试打招呼")
                self.assertEqual(pet._settings.total_pats, pat_before)
                self.assertEqual(pet._override_pose, turn2_pose)
                self.assertFalse(pet._is_drag_float_allowed())
                self.assertFalse(pet._process_drag_shake_step(80.0, 0.0, time.monotonic(), 0.0))

                # 双击身体仍直接放行查看额度，并允许播放额度查询动作，查完过期后恢复本轮固定工作动作。
                quota_calls = []
                pet.open_quota_requested.connect(lambda: quota_calls.append(True))
                body_rect = pet._get_local_sprite_rect()
                body_pt = QPointF(body_rect.center().x(), body_rect.top() + body_rect.height() * 0.75)
                dbl = QMouseEvent(
                    QEvent.Type.MouseButtonDblClick,
                    body_pt,
                    body_pt,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                pet._on_stage_mouse_double_click(dbl)
                self.assertEqual(len(quota_calls), 1)

                pet.react_quota_lookup_started()
                self.assertEqual(pet._override_pose, PetState.SEARCHING)
                pet.react_quota_lookup_finished(SimpleNamespace(success=True))
                self.assertEqual(pet._override_pose, PetState.HAPPY)
                # 额度动作播完后若仍在工作期，自动回到本轮固定工作动作。
                pet._override_until_utc = datetime.now(timezone.utc) - timedelta(seconds=1)
                pet._frame_timer_tick()
                self.assertEqual(pet._override_pose, turn2_pose)

                # 工作屏蔽期放行贴边吸附：拖至屏幕左侧松手进入贴边态，同步事件不覆盖贴边形象，拖回桌面恢复工作动作。
                from models.pet_settings import DockSide

                area = pet._get_work_area(pet.x() + pet.width() * 0.5, pet.y() + pet.height() * 0.5)
                srect = pet._get_local_sprite_rect()
                floor_top = float(area.bottom() + 1 - pet.height())
                pet._settings.edge_dock_enabled = True
                dock_y = int(round(floor_top))
                pet.move(int(round(area.left() - (pet.width() - srect.width()) * 0.5 + 8.0)), dock_y)
                pet._mouse_down = True
                pet._is_dragging = True
                rel_ev = QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    QPointF(100.0, 100.0),
                    QPointF(float(area.left() + 40), float(dock_y + 120)),
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                pet._on_stage_mouse_release(rel_ev)
                self.assertEqual(pet._settings.docked_side, DockSide.LEFT)
                self.assertIsNone(pet._override_pose)
                self.assertIn(pet._visual_state, (PetState.EDGE_LEFT, PetState.EDGE_LEFT_PEEK))
                # 贴边期间继续收到工作事件与帧刷新，仍保持贴边状态与同步气泡不被覆盖。
                pet._show_sync_event(SyncEvent("system", "贴边中工具调用", "tool"))
                pet._frame_timer_tick()
                self.assertEqual(pet._settings.docked_side, DockSide.LEFT)
                self.assertIsNone(pet._override_pose)
                self.assertEqual(pet._bubble_text_lbl.text(), "⚙ 贴边中工具调用")
                # 从侧边拖回桌面松手：恢复本轮固定工作动作。
                pet._mouse_down = True
                pet._is_dragging = False
                pet._drag_start_global = QPointF(float(area.left() + 40), float(dock_y + 120))
                pet._last_move_global = QPointF(float(area.left() + 40), float(dock_y + 120))
                pet._win_start_left = float(pet.x())
                pet._win_start_top = float(pet.y())
                move_ev = QMouseEvent(
                    QEvent.Type.MouseMove,
                    QPointF(100.0, 100.0),
                    QPointF(float(area.left() + 280), float(dock_y + 120)),
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                pet._on_stage_mouse_move(move_ev)
                self.assertEqual(pet._settings.docked_side, DockSide.NONE)
                self.assertEqual(pet._override_pose, turn2_pose)
                pet._on_stage_mouse_release(rel_ev)
                self.assertEqual(pet._settings.docked_side, DockSide.NONE)
                self.assertEqual(pet._override_pose, turn2_pose)
                self.assertFalse(pet._is_falling)

                # 4. 锁定交互动作播放期与打盹模式下，同步事件不得强行打断当前动作。
                pet._set_temporary_pose(PetState.REDHOT, 5.0, locked=True)
                pet._show_sync_event(SyncEvent("system", "工具调用中", "tool"))
                self.assertEqual(pet._override_pose, PetState.REDHOT)
                pet._override_locked = False
                pet._override_pose = None
                pet._settings.doze_mode = True
                pet._show_sync_event(SyncEvent("assistant", "打盹期间完成", "message"))
                self.assertIsNone(pet._override_pose)
                pet._settings.doze_mode = False

                # 5. 回复完成结束后释放全部交互动作，且同步气泡关闭后普通消息气泡逻辑恢复正常。
                pet._show_sync_event(SyncEvent("assistant", "回复完成，准备结束播报", "message"))
                app.processEvents()
                self.assertEqual(pet._override_pose, _SYNC_DONE_POSE)
                self.assertFalse(pet._is_sync_working_locked())
                self.assertFalse(pet._sync_waiting_for_reply)
                pet._on_bubble_timeout()
                app.processEvents()
                self.assertFalse(pet._sync_bubble_active)
                pet.trigger_pat()
                self.assertEqual(pet._override_pose, PetState.PAT)
                self.assertEqual(pet._settings.total_pats, pat_before + 1)
                pet.show_bubble("恢复正常气泡消息", 3.0, force=False)
                app.processEvents()
                self.assertTrue(pet._speech_bubble.isVisible())
                self.assertEqual(pet._bubble_text_lbl.text(), "恢复正常气泡消息")

                # 6. 多对话并发工作：其中一个完成时刷新气泡但保持工作动作与交互屏蔽，全部完成后才切开心并释放。
                pet._override_pose = None
                pet._override_is_autonomous = False
                pet._override_is_sync = False
                pet._show_sync_event(SyncEvent("user", "并发任务 A", "message", source_id="conv_a"))
                app.processEvents()
                multi_pose = pet._override_pose
                self.assertIn(multi_pose, _SYNC_BUSY_POSES)
                self.assertTrue(pet._is_sync_working_locked())

                pet._show_sync_event(SyncEvent("user", "并发任务 B", "message", source_id="conv_b"))
                app.processEvents()
                self.assertEqual(pet._override_pose, multi_pose)
                self.assertEqual(pet._sync_working_sessions, {"conv_a", "conv_b"})

                # 会话 B 先完成：气泡刷新为 B 的回复，但因为 A 仍在工作，不退出工作动作也不释放摸头。
                pet._show_sync_event(SyncEvent("assistant", "任务 B 完成啦", "message", source_id="conv_b"))
                app.processEvents()
                self.assertEqual(pet._bubble_text_lbl.text(), "任务 B 完成啦")
                self.assertEqual(pet._override_pose, multi_pose)
                self.assertTrue(pet._is_sync_working_locked())
                self.assertEqual(pet._sync_working_sessions, {"conv_a"})
                pats_mid = pet._settings.total_pats
                pet.trigger_pat()
                self.assertEqual(pet._settings.total_pats, pats_mid)

                # 会话 A 也完成后：所有工作会话结束，切开心（hum）、刷新气泡并释放交互。
                pet._show_sync_event(SyncEvent("assistant", "任务 A 也完成啦", "message", source_id="conv_a"))
                app.processEvents()
                self.assertEqual(pet._bubble_text_lbl.text(), "任务 A 也完成啦")
                self.assertEqual(pet._override_pose, _SYNC_DONE_POSE)
                self.assertFalse(pet._is_sync_working_locked())
                self.assertEqual(pet._sync_working_sessions, set())
                pet.trigger_pat()
                self.assertEqual(pet._override_pose, PetState.PAT)
                self.assertEqual(pet._settings.total_pats, pats_mid + 1)

                # 7. 用户取消或无工作流程空闲自动取消时，回归正常待机且不切开心动作，同时释放交互与气泡常亮。
                pet._override_pose = None
                pet._override_is_autonomous = False
                pet._override_is_sync = False
                pet._show_sync_event(SyncEvent("system", "正在思考中", "thinking", source_id="conv_cancel"))
                app.processEvents()
                self.assertIn(pet._override_pose, _SYNC_BUSY_POSES)
                self.assertTrue(pet._is_sync_working_locked())
                self.assertTrue(pet._sync_waiting_for_reply)

                pet._apply_sync_working_snapshot(set(), completed=False, has_pending_events=False)
                app.processEvents()
                self.assertIsNone(pet._override_pose)
                self.assertFalse(pet._is_sync_working_locked())
                self.assertFalse(pet._sync_waiting_for_reply)
                self.assertTrue(pet._bubble_timer.isActive())
            finally:
                pet.close()


if __name__ == "__main__":
    unittest.main()

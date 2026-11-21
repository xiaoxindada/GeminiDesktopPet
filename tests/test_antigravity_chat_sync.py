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


if __name__ == "__main__":
    unittest.main()

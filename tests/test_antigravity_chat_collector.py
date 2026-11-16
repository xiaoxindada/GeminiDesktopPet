"""反重力同步采集器：增量、续写、去重、失败识别与自动跟随验证。"""

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
            status.update({name: data for name, data in self.state.take_since(self.baseline) if name == "status"})
        self.assertTrue(status["status"]["switched"])
        self.assertEqual(status["status"]["title"], "新的会话")


if __name__ == "__main__":
    unittest.main()

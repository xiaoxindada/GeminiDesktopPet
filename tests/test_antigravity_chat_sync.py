"""Antigravity 会话库的消息、思考与工具事件提取验证。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.antigravity_chat_sync_service import AntigravityChatSyncService


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
            user_payload = _bytes_field(19, _bytes_field(2, "示例问题".encode())) + _step_time(1794541874)
            answer_payload = (
                _bytes_field(20, _bytes_field(3, "思考示例".encode()))
                + _bytes_field(20, _bytes_field(1, "示例回答".encode()))
                + _step_time(1794541885)
            )
            tool_metadata = _bytes_field(
                4,
                _bytes_field(2, b"run_command")
                + _bytes_field(3, json.dumps({"toolSummary": "查看状态"}, ensure_ascii=False).encode()),
            )
            tool_payload = _bytes_field(
                140,
                _bytes_field(2, _bytes_field(1, "Command exited with code 0. Command output: ok".encode())),
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
            self.assertEqual([message.role for message in conversation.messages[:3]], ["user", "system", "assistant"])
            self.assertEqual(conversation.messages[1].text, "思考示例")
            self.assertEqual(conversation.messages[2].text, "示例回答")
            tool_text = conversation.messages[3].text
            self.assertIn("run_command", tool_text)
            self.assertIn("查看状态", tool_text)
            self.assertIn("退出码 0", tool_text)
            self.assertTrue(conversation.messages[0].time.startswith("2026-"))
            self.assertEqual(conversation.title, "示例问题")

    def test_repeated_thinking_text_is_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repeat.db"
            step = (
                _bytes_field(20, _bytes_field(3, "重复思考".encode()))
                + _bytes_field(20, _bytes_field(1, "回答".encode()))
            )
            _create_db(path, [(0, 15, b"", step), (1, 15, b"", step)])

            conversation = AntigravityChatSyncService.read_conversation(path)
            thinking = [message for message in conversation.messages if message.kind == "thinking"]
            self.assertEqual(len(thinking), 1)
            self.assertEqual(thinking[0].text, "重复思考")

    def test_latest_conversation_skips_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _create_db(directory / "empty.db", [])
            user_payload = _bytes_field(19, _bytes_field(2, "有内容的会话".encode()))
            _create_db(directory / "full.db", [(0, 14, b"", user_payload)])

            service = AntigravityChatSyncService((directory,))
            conversation = service.latest_conversation()
            self.assertIsNotNone(conversation)
            assert conversation is not None
            self.assertEqual(conversation.source_id, "full")
            self.assertEqual(conversation.messages[0].text, "有内容的会话")


if __name__ == "__main__":
    unittest.main()

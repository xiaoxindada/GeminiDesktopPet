"""反重力会话库的增量解析：只解尾部也要和全解给出同样的结果。"""

from __future__ import annotations

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


def _user(text: str) -> bytes:
    return _bytes_field(19, _bytes_field(2, text.encode())) + _step_time(1794541874)


def _assistant(text: str, thinking: str = "") -> bytes:
    payload = b""
    if thinking:
        payload += _bytes_field(20, _bytes_field(3, thinking.encode()))
    return payload + _bytes_field(20, _bytes_field(1, text.encode())) + _step_time(1794541885)


def _create_db(path: Path, rows: list[tuple[int, int, bytes]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "create table steps (idx integer, step_type integer, metadata blob, step_payload blob)"
        )
        connection.executemany(
            "insert into steps values (?, ?, ?, ?)",
            [(idx, step_type, b"", payload) for idx, step_type, payload in rows],
        )
        connection.commit()
    finally:
        connection.close()


class TestIncrementalDecode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "s1.db"
        _create_db(
            self.path,
            [
                (0, 14, _user("问题一")),
                (1, 15, _assistant("回答一", "想一想")),
                (2, 14, _user("问题二")),
                (3, 15, _assistant("回答二")),
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


if __name__ == "__main__":
    unittest.main()

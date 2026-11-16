"""带版本号的状态叶子：采集器写入，界面按 counter 读差量。

状态只有一个出口——采集器写叶子，界面拉「自上次以来变过的叶子」，
不再各自维护游标。首次读取只记基线不消费，避免把历史重放一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Optional


@dataclass(frozen=True)
class StateLeaf:
    """一次写入：整份状态里递增的编号 + 载荷。"""

    counter: int
    data: object


class ChatSyncState:
    """单槽叶子集合：同名叶子后写覆盖先写，编号只增不减。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counter = 0
        self._leaves: dict[str, StateLeaf] = {}

    @property
    def version(self) -> int:
        """整份状态的版本号，等于最后一次写入的编号。"""
        with self._lock:
            return self._counter

    def write(self, name: str, data: object) -> int:
        with self._lock:
            self._counter += 1
            self._leaves[name] = StateLeaf(self._counter, data)
            return self._counter

    def read(self, name: str) -> Optional[StateLeaf]:
        with self._lock:
            return self._leaves.get(name)

    def counters(self) -> dict[str, int]:
        """各叶子的当前编号，用作差量基线。"""
        with self._lock:
            return {name: leaf.counter for name, leaf in self._leaves.items()}

    def take_since(self, baseline: dict[str, int]) -> list[tuple[str, object]]:
        """返回基线之后变化过的叶子，并就地推进基线。"""
        with self._lock:
            changes = [
                (name, leaf.data)
                for name, leaf in self._leaves.items()
                if baseline.get(name) != leaf.counter
            ]
            for name, leaf in self._leaves.items():
                baseline[name] = leaf.counter
        return changes

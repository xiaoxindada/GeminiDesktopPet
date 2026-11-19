"""独立无头后台跟随守护入口：打包为无 PySide6 依赖的 FollowWatcher.exe，拷贝到用户目录运行以避免占用主程序目录。"""

from __future__ import annotations

import os
import sys
from typing import Optional

from services.app_follow_service import AppFollowService


def parse_watcher_args(argv: list[str]) -> tuple[bool, Optional[str]]:
    """解析 --from-exit 与 --target-exe 参数。"""
    from_exit = False
    target_path: Optional[str] = None
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        low = arg.lower()
        if low == "--from-exit":
            from_exit = True
        elif low == "--target-exe" and idx + 1 < len(argv):
            target_path = argv[idx + 1].strip() or None
            idx += 1
        elif low.startswith("--target-exe="):
            target_path = arg.split("=", 1)[1].strip() or None
        idx += 1
    return from_exit, target_path


def main(args: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    from_exit, target_path = parse_watcher_args(argv)
    if target_path:
        target_path = os.path.abspath(target_path)
    return AppFollowService.run_follow_watcher(from_exit, target_path=target_path)


if __name__ == "__main__":
    raise SystemExit(main())

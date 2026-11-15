"""Gemini 娘 · Windows 桌面萌宠主入口。"""

from __future__ import annotations

import multiprocessing
import os
import sys

from models.app_version import APP_VERSION

__version__ = APP_VERSION

# 确保冻结与多屏 DPI 下先设置 freeze_support 与 Qt 环境变量
multiprocessing.freeze_support()
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")


def _entrypoint() -> int:
    from cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_entrypoint())

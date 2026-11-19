"""CLI 子命令快速分发入口：无须加载 PySide6 即可处理关闭、更新和后台守护模式。"""

from __future__ import annotations

import multiprocessing
import os
import sys
from typing import Optional

from models.app_version import AppVersion

from services.app_follow_service import AppFollowService
from services.app_logger import AppLogger
from services.settings_service import SettingsService


def _handle_apply_update(args: list[str]) -> int:
    low = [a.lower() for a in args]
    try:
        idx = low.index("--apply-update")
    except ValueError:
        return 1

    if idx + 2 >= len(args):
        return 1

    try:
        parent_pid = int(args[idx + 1])
    except ValueError:
        parent_pid = 0
    target_dir = args[idx + 2]

    from services.update_service import UpdateService

    settings = SettingsService().load()
    AppLogger.initialize(settings.enable_runtime_log)
    ok = UpdateService.apply_staged_update_and_relaunch(parent_pid, target_dir)
    return 0 if ok else 1


def _handle_close_pet(args: list[str]) -> int:
    low = {a.lower() for a in args}
    AppFollowService.stop_all_instances(
        stop_all="--stop-all" in low,
        force="--force" in low,
    )
    return 0


def _handle_disable_follow() -> int:
    service = SettingsService()
    settings = service.load()
    settings.follow_antigravity = False
    settings.follow_antigravity_cli = False
    settings.follow_gemini_app = False
    service.save(settings)
    AppFollowService(service).disable_follow()
    return 0


def main(args: Optional[list[str]] = None) -> int:
    """根据启动参数选择轻量无头任务或启动完整 PySide6 桌宠界面。"""
    argv = list(sys.argv[1:] if args is None else args)
    low = [a.lower() for a in argv]

    if "-h" in low or "--help" in low:
        print(
            "GeminiDesktopPet CLI / 桌面萌宠启动入口\n"
            "用法:\n"
            "  gemini-desktop-pet [选项]\n"
            "  python main.py [选项]\n\n"
            "常用选项:\n"
            "  --close-pet           通知运行中的主实例优雅退出\n"
            "  --stop-all            停止主实例以及后台跟随守护进程\n"
            "  --force               配合 --close-pet 强制结束残留进程\n"
            "  --disable-follow      关闭应用跟随联动并停止守护进程\n"
            "  --follow-watcher      以无头后台跟随守护模式运行\n"
            "  --version, -V         显示当前版本号"
        )
        return 0

    if "-v" in low or "--version" in low:
        print(f"GeminiDesktopPet {AppVersion.current_display()}")
        return 0

    if "--apply-update" in low:
        return _handle_apply_update(argv)

    if "--close-pet" in low:
        return _handle_close_pet(argv)

    if "--disable-follow" in low:
        return _handle_disable_follow()

    if "--follow-watcher" in low:
        from follow_watcher import parse_watcher_args

        from_exit, target_path = parse_watcher_args(argv)
        return AppFollowService.run_follow_watcher(from_exit, target_path=target_path)

    # 通过 console/gui entry point 启动时也补齐 freeze_support 与高 DPI 变量
    multiprocessing.freeze_support()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    # 仅在进入交互式桌面宠界面时才导入 PySide6，保持无头子命令秒级完成与极低内存
    from app import run_gui_app

    return run_gui_app(argv)


if __name__ == "__main__":
    raise SystemExit(main())

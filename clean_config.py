"""GeminiDesktopPet 用户配置清理工具（可打包为独立 CleanConfig.exe 随程序发布）。"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from models.app_version import AppVersion
from services.config_cleanup_service import CleanupTargets, ConfigCleanupService


def _normalize_argv(argv: list[str]) -> list[str]:
    # 兼容 Windows 习惯的 /y 参数
    return ["--yes" if arg.lower() == "/y" else arg for arg in argv]


def _pause_if_interactive(auto_yes: bool) -> None:
    if auto_yes:
        return
    try:
        input("\n按回车键退出...")
    except EOFError:
        pass


def _print_plan(targets: CleanupTargets) -> None:
    print("========================================================")
    print(f"  GeminiDesktopPet 配置清理工具 ({AppVersion.current_display()})")
    print("========================================================")
    print("将停止运行中的 GeminiDesktopPet 进程，并清理以下内容：")
    print(f"  1. 统一用户配置与数据目录 : {targets.user_config_dir}")
    print("     （包含 user_config.json、character.json、Logs、sounds、updates、PID 标记）")
    print("  2. 用户启动目录开机快捷方式（若存在）")
    print()


def main(args: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            enc = sys.stdout.encoding or "utf-8"
            if sys.platform == "win32" and not sys.stdout.isatty():
                import ctypes

                if ctypes.WinDLL("kernel32").GetConsoleOutputCP() == 65001:
                    enc = "utf-8"
            sys.stdout.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="CleanConfig",
        description="清理 GeminiDesktopPet 用户配置、本地缓存与启动目录快捷方式。",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过交互确认与退出暂停，直接执行清理。",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="store_true",
        help="显示版本号并退出。",
    )
    parsed = parser.parse_args(_normalize_argv(list(sys.argv[1:] if args is None else args)))

    if parsed.version:
        print(f"GeminiDesktopPet CleanConfig {AppVersion.current_display()}")
        return 0

    targets = ConfigCleanupService.resolve_default_targets()

    if not parsed.yes:
        _print_plan(targets)
        try:
            answer = input("确认继续清理? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消，未删除任何文件。")
            _pause_if_interactive(False)
            return 0

    report = ConfigCleanupService.run(targets)

    print()
    print("--- 清理结果 ---")
    for path in report.removed_paths:
        print(f"  [已清理] {path}")
    for path in report.missing_paths:
        print(f"  [不存在] {path}")
    for path, err in report.failed_paths:
        print(f"  [失败] {path}: {err}")

    if report.failed_paths:
        print("\n部分项目未能清理，请确认无其他程序占用后重试。")
        _pause_if_interactive(parsed.yes)
        return 1

    print("\n清理完成，现在可直接删除 GeminiDesktopPet 程序目录。")
    _pause_if_interactive(parsed.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

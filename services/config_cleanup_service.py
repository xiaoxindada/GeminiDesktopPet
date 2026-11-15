"""统一用户目录（%USERPROFILE%/.GeminiDesktopPet）与启动目录快捷方式清理服务。"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
from typing import Optional

from models.app_version import get_user_config_dir
from services.app_follow_service import AppFollowService
from services.settings_service import SettingsService


@dataclass(frozen=True)
class CleanupTargets:
    """描述需要清理的统一用户目录与开机自启快捷方式范围。"""

    user_config_dir: Path
    startup_shortcuts: tuple[Path, ...] = ()


@dataclass
class CleanupReport:
    """汇总清理结果，便于 CLI 输出与单元测试断言。"""

    removed_paths: list[Path] = field(default_factory=list)
    missing_paths: list[Path] = field(default_factory=list)
    failed_paths: list[tuple[Path, str]] = field(default_factory=list)


class ConfigCleanupService:
    """在删除/卸载桌面宠前统一清理 ~/.GeminiDesktopPet 用户目录与启动快捷方式。"""

    @staticmethod
    def resolve_default_targets() -> CleanupTargets:
        return CleanupTargets(
            user_config_dir=get_user_config_dir(),
            startup_shortcuts=(
                SettingsService.get_startup_shortcut_path(),
                AppFollowService.get_follow_watcher_shortcut_path(),
            ),
        )

    @classmethod
    def run(
        cls,
        targets: Optional[CleanupTargets] = None,
        *,
        stop_instances: bool = True,
    ) -> CleanupReport:
        resolved = targets or cls.resolve_default_targets()
        report = CleanupReport()

        # 先停止正在运行的主进程与后台守护，释放文件占用
        if stop_instances:
            try:
                AppFollowService.stop_all_instances(stop_all=True, force=True)
            except Exception:
                pass

        cls._remove_dir(resolved.user_config_dir, report, record_missing=True)

        for shortcut_path in resolved.startup_shortcuts:
            cls._remove_file(shortcut_path, report)

        return report

    @classmethod
    def _remove_dir(cls, folder: Path, report: CleanupReport, *, record_missing: bool) -> None:
        if not folder.exists():
            if record_missing:
                report.missing_paths.append(folder)
            return
        try:
            shutil.rmtree(folder)
            report.removed_paths.append(folder)
        except PermissionError:
            # 清除只读属性后重试一次，兼容只读缓存/配置文件
            cls._clear_readonly_recursive(folder)
            try:
                shutil.rmtree(folder)
                report.removed_paths.append(folder)
            except OSError as exc:
                report.failed_paths.append((folder, str(exc)))
        except OSError as exc:
            report.failed_paths.append((folder, str(exc)))

    @staticmethod
    def _remove_file(file_path: Path, report: CleanupReport) -> None:
        if not file_path.is_file():
            return
        try:
            os.chmod(file_path, stat.S_IWRITE)
            file_path.unlink()
            report.removed_paths.append(file_path)
        except OSError as exc:
            report.failed_paths.append((file_path, str(exc)))

    @staticmethod
    def _clear_readonly_recursive(folder: Path) -> None:
        for root, dirs, files in os.walk(folder):
            for name in (*dirs, *files):
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
                except OSError:
                    pass

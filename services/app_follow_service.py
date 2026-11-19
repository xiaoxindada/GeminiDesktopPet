"""应用跟随联动待机引擎、跨进程唤醒/停机信令及冷启动后台守护（--follow-watcher）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

import psutil

from models.app_version import get_app_base_dir, get_bundled_resource_dir, get_user_config_dir
from models.pet_settings import PetSettings
from services.app_logger import AppLogger
from services.followed_app_probe import AppActivitySnapshot, FollowedAppProbe
from services.settings_service import SettingsService
from services.win32_ipc import (
    CLOSE_EVENT_NAME,
    MAIN_MUTEX_NAME,
    STOP_ALL_EVENT_NAME,
    STOP_WATCHER_EVENT_NAME,
    WATCHER_MUTEX_NAME,
    Win32Event,
    Win32Mutex,
    get_launch_command,
    get_project_entry_script,
    get_windows_startup_dir,
    is_startup_entry_enabled,
    set_startup_entry_enabled,
    subprocess_hidden_kwargs,
    wait_any_events,
)

FOLLOW_STARTUP_SHORTCUT_NAME = "GeminiDesktopPet_FollowWatcher.lnk"
FOLLOW_WATCHER_EXE_NAME = "FollowWatcher.exe"
STAGED_WATCHER_DIR_NAME = "watcher"
STAGED_WATCHER_EXE_NAME = "GeminiDesktopPet_FollowWatcher.exe"
_MAIN_PID_FILE = "main.pid"
_WATCHER_PID_FILE = "watcher.pid"
_PET_PROCESS_NAMES = frozenset(
    {
        "geminidesktoppet.exe",
        "geminidesktoppet",
        "geminidesktoppet_followwatcher.exe",
        "geminidesktoppet_followwatcher",
        "followwatcher.exe",
        "followwatcher",
    }
)


@dataclass
class FollowWatcherOptions:
    from_exit: bool = False
    target_path: Optional[str] = None
    custom_settings_folder: Optional[str] = None
    main_mutex_name: str = MAIN_MUTEX_NAME
    watcher_mutex_name: str = WATCHER_MUTEX_NAME
    stop_watcher_event_name: str = STOP_WATCHER_EVENT_NAME
    stop_all_event_name: str = STOP_ALL_EVENT_NAME
    poll_interval_ms: int = 1200
    sample_antigravity: Optional[Callable[[], AppActivitySnapshot]] = None
    sample_gemini: Optional[Callable[[], AppActivitySnapshot]] = None
    sample_antigravity_cli: Optional[Callable[[], AppActivitySnapshot]] = None
    custom_wake_launcher: Optional[Callable[[str], bool]] = None


def _get_pid_marker_path(filename: str) -> str:
    return os.path.join(str(get_user_config_dir()), filename)


def _is_pet_process(proc: psutil.Process) -> bool:
    """判断进程是否属于本桌宠或其后台守护（兼容打包态与源码态）。"""
    try:
        name = (proc.name() or "").lower()
        if name in _PET_PROCESS_NAMES:
            return True
        if name.startswith("python"):
            entry_norm = os.path.normcase(os.path.abspath(get_project_entry_script()))
            for arg in proc.cmdline() or ():
                if os.path.normcase(os.path.abspath(arg)) == entry_norm or os.path.basename(arg).lower() in {
                    "main.py",
                    "follow_watcher.py",
                }:
                    return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    return False


class AppFollowService:
    """管理进程级跟随唤醒待机、冷启动守护进程交接与启动目录快捷方式。"""

    def __init__(self, settings_service: SettingsService) -> None:
        self._settings_service = settings_service
        os.makedirs(settings_service.settings_folder, exist_ok=True)

    @staticmethod
    def any_follow_enabled(settings: PetSettings) -> bool:
        return bool(settings.follow_antigravity or settings.follow_gemini_app or settings.follow_antigravity_cli)

    @classmethod
    def get_follow_watcher_shortcut_path(cls) -> Path:
        return get_windows_startup_dir() / FOLLOW_STARTUP_SHORTCUT_NAME

    @classmethod
    def is_follow_watcher_run_enabled(cls) -> bool:
        return is_startup_entry_enabled(FOLLOW_STARTUP_SHORTCUT_NAME)

    @classmethod
    def get_bundled_watcher_exe_path(cls) -> Optional[Path]:
        """定位打包发布的轻量守护可执行文件（优先 _internal/FollowWatcher.exe）。"""
        if not getattr(sys, "frozen", False):
            return None
        base_dir = Path(get_app_base_dir())
        for candidate in (
            base_dir / "_internal" / FOLLOW_WATCHER_EXE_NAME,
            base_dir / FOLLOW_WATCHER_EXE_NAME,
            Path(get_bundled_resource_dir()) / FOLLOW_WATCHER_EXE_NAME,
        ):
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def get_staged_watcher_dir(cls) -> Path:
        return get_user_config_dir() / STAGED_WATCHER_DIR_NAME

    @classmethod
    def get_staged_watcher_exe_path(cls) -> Path:
        return cls.get_staged_watcher_dir() / STAGED_WATCHER_EXE_NAME

    @staticmethod
    def _files_identical(src: Path, dst: Path) -> bool:
        try:
            if not dst.is_file():
                return False
            if src.stat().st_size != dst.stat().st_size:
                return False
            h_src = hashlib.sha256()
            h_dst = hashlib.sha256()
            with src.open("rb") as fs, dst.open("rb") as fd:
                for chunk in iter(lambda: fs.read(131072), b""):
                    h_src.update(chunk)
                for chunk in iter(lambda: fd.read(131072), b""):
                    h_dst.update(chunk)
            return h_src.digest() == h_dst.digest()
        except OSError:
            return False

    @classmethod
    def ensure_staged_watcher_exe(cls) -> Optional[Path]:
        """将独立轻量守护同步至用户配置目录，使后台监听运行期间完全不占用主程序目录。"""
        source = cls.get_bundled_watcher_exe_path()
        if source is None:
            return None

        staged_dir = cls.get_staged_watcher_dir()
        staged_exe = cls.get_staged_watcher_exe_path()
        if cls._files_identical(source, staged_exe):
            return staged_exe

        try:
            staged_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            AppLogger.warn("AppFollow", f"创建后台守护运行目录失败：{exc}")
            return None

        tmp_path = staged_dir / f"{STAGED_WATCHER_EXE_NAME}.{os.getpid()}.tmp"
        for attempt in range(1, 7):
            try:
                shutil.copy2(source, tmp_path)
                os.replace(tmp_path, staged_exe)
                return staged_exe
            except (OSError, PermissionError) as exc:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                if attempt == 6:
                    AppLogger.warn("AppFollow", f"同步用户目录后台守护失败：{exc}")
                    return staged_exe if staged_exe.is_file() else None
                time.sleep(0.1)
        return None

    @classmethod
    def cleanup_staged_watcher_exe(cls) -> None:
        staged_dir = cls.get_staged_watcher_dir()
        if not staged_dir.exists():
            return
        try:
            shutil.rmtree(staged_dir)
        except OSError:
            pass

    @classmethod
    def get_follow_watcher_launch_spec(
        cls, *, from_exit: bool, target_path: Optional[str] = None
    ) -> tuple[list[str], str]:
        """构造后台守护的启动命令与解耦工作目录（打包态优先运行用户目录副本）。"""
        staged_exe = cls.ensure_staged_watcher_exe()
        if staged_exe is not None:
            resolved_target = os.path.abspath(target_path or sys.executable)
            args = ["--follow-watcher"]
            if from_exit:
                args.append("--from-exit")
            args.extend(["--target-exe", resolved_target])
            return [str(staged_exe), *args], str(staged_exe.parent)

        args = ["--follow-watcher", "--from-exit"] if from_exit else ["--follow-watcher"]
        if target_path:
            args.extend(["--target-exe", os.path.abspath(target_path)])
        cmd = get_launch_command(args, gui=True)
        cwd = str(get_user_config_dir())
        return cmd, cwd

    @classmethod
    def set_follow_watcher_run(cls, enable: bool) -> bool:
        if enable:
            staged_exe = cls.ensure_staged_watcher_exe()
            if staged_exe is not None:
                target_exe = os.path.abspath(sys.executable)
                return set_startup_entry_enabled(
                    FOLLOW_STARTUP_SHORTCUT_NAME,
                    True,
                    args=["--follow-watcher", "--target-exe", target_exe],
                    description="GeminiDesktopPet 后台跟随联动待机",
                    icon_path=str(staged_exe),
                    working_dir=str(staged_exe.parent),
                    target_override=str(staged_exe),
                )

        icon_candidate = Path(get_bundled_resource_dir()) / "Assets" / "AppIcon.ico"
        return set_startup_entry_enabled(
            FOLLOW_STARTUP_SHORTCUT_NAME,
            enable,
            args=["--follow-watcher"],
            description="GeminiDesktopPet 后台跟随联动待机",
            icon_path=str(icon_candidate) if icon_candidate.is_file() else None,
            working_dir=str(get_user_config_dir()) if enable else get_app_base_dir(),
        )

    def is_follow_logon_enabled(self) -> bool:
        return self.is_follow_watcher_run_enabled() or SettingsService.is_startup_enabled()

    def sync_and_apply(self, settings: PetSettings) -> None:
        enabled = self.any_follow_enabled(settings)
        # 主界面自带联动监听，先停止旧后台守护以释放用户目录副本，再同步启动入口。
        self.stop_follow_watcher()
        self.set_follow_watcher_run(enabled)
        AppLogger.info(
            "AppFollow",
            f"同步应用联动状态：Antigravity={settings.follow_antigravity}, GeminiApp={settings.follow_gemini_app}, AntigravityCLI={settings.follow_antigravity_cli}, StartupShortcut={enabled}",
        )

    def disable_follow(self) -> None:
        self.set_follow_watcher_run(False)
        self.stop_follow_watcher()
        self.cleanup_staged_watcher_exe()
        AppLogger.info("AppFollow", "已停用应用跟随联动并清除开机待机入口")

    @staticmethod
    def try_signal_named_event(event_name: str) -> bool:
        return Win32Event.open_and_set(event_name)

    @staticmethod
    def is_stop_all_signaled(stop_all_event_name: str = STOP_ALL_EVENT_NAME) -> bool:
        # 仅通过 OpenEventW 打开已存在的命名事件，避免检查信号时意外创建事件句柄
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenEventW(0x00100000, False, stop_all_event_name)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def acquire_stop_all_signal(stop_all_event_name: str = STOP_ALL_EVENT_NAME) -> Win32Event:
        ev = Win32Event(stop_all_event_name, manual_reset=True, initial_state=True)
        Win32Event.open_and_set(stop_all_event_name)
        return ev

    @staticmethod
    def is_main_instance_running(main_mutex_name: str = MAIN_MUTEX_NAME) -> bool:
        return Win32Mutex.exists(main_mutex_name)

    @staticmethod
    def is_follow_watcher_running(watcher_mutex_name: str = WATCHER_MUTEX_NAME) -> bool:
        return Win32Mutex.exists(watcher_mutex_name)

    @staticmethod
    def record_main_pid(pid: int) -> None:
        AppFollowService._write_pid_marker(_MAIN_PID_FILE, pid)

    @staticmethod
    def clear_main_pid(pid: int) -> None:
        AppFollowService._clear_pid_marker(_MAIN_PID_FILE, pid)

    @staticmethod
    def record_watcher_pid(pid: int) -> None:
        AppFollowService._write_pid_marker(_WATCHER_PID_FILE, pid)

    @staticmethod
    def clear_watcher_pid(pid: int) -> None:
        AppFollowService._clear_pid_marker(_WATCHER_PID_FILE, pid)

    @staticmethod
    def clear_all_pid_markers() -> None:
        for fn in (_MAIN_PID_FILE, _WATCHER_PID_FILE):
            p = _get_pid_marker_path(fn)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    @staticmethod
    def try_get_live_main_process() -> Optional[psutil.Process]:
        return AppFollowService._get_recorded_live_process(_MAIN_PID_FILE)

    @staticmethod
    def try_get_live_watcher_process() -> Optional[psutil.Process]:
        return AppFollowService._get_recorded_live_process(_WATCHER_PID_FILE)

    @staticmethod
    def get_other_pet_processes() -> list[psutil.Process]:
        self_pid = os.getpid()
        excluded_pids: set[int] = {self_pid}
        try:
            for parent in psutil.Process(self_pid).parents():
                excluded_pids.add(parent.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass

        result: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.pid in excluded_pids:
                    continue
                if _is_pet_process(proc) and proc.is_running():
                    try:
                        cmd_low = [a.lower() for a in (proc.cmdline() or ())]
                        if "--close-pet" in cmd_low:
                            continue
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        pass
                    result.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return result

    @staticmethod
    def kill_process_tree_safe(proc: psutil.Process) -> None:
        try:
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except Exception:
                    pass
            proc.kill()
        except Exception:
            pass

    @staticmethod
    def wait_for_process_exit(proc: psutil.Process, timeout_ms: int) -> bool:
        try:
            proc.wait(timeout=max(0.01, timeout_ms / 1000.0))
            return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.TimeoutExpired, psutil.AccessDenied, OSError):
            return False

    @staticmethod
    def _find_onefile_bootloader_parent(proc: object) -> Optional[psutil.Process]:
        """识别 PyInstaller --onefile 外层引导进程（与子进程共用同一 exe 路径）。"""
        if not isinstance(proc, psutil.Process):
            return None
        try:
            parent = proc.parent()
            if parent is None or parent.pid == os.getpid():
                return None
            if os.path.normcase(os.path.abspath(parent.exe())) == os.path.normcase(os.path.abspath(proc.exe())):
                return parent
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        return None

    @classmethod
    def stop_follow_watcher(cls, timeout_ms: int = 800) -> None:
        # 保留停止事件直到等待结束，并在发信号前取得进程，避免 PID 标记被退出中的守护先清除。
        stop_event = Win32Event(STOP_WATCHER_EVENT_NAME, manual_reset=True)
        try:
            watcher = cls.try_get_live_watcher_process()
            bootloader = cls._find_onefile_bootloader_parent(watcher)
            stop_event.set()
            deadline = time.monotonic() + timeout_ms / 1000.0
            while watcher is None and cls.is_follow_watcher_running() and time.monotonic() < deadline:
                time.sleep(0.025)
                watcher = cls.try_get_live_watcher_process()
                if watcher is not None:
                    bootloader = cls._find_onefile_bootloader_parent(watcher)
            if watcher is not None and not cls.wait_for_process_exit(watcher, timeout_ms):
                # 守护可能刚启动桌宠，强制回收时只能结束守护本身。
                watcher.kill()
                if not cls.wait_for_process_exit(watcher, 1000):
                    AppLogger.warn("AppFollow", "等待后台联动守护退出超时")
            # 若是 PyInstaller --onefile 守护，同时等待外层引导进程释放临时目录与 exe 句柄。
            if bootloader is not None and not cls.wait_for_process_exit(bootloader, max(timeout_ms, 1500)):
                bootloader.kill()
                cls.wait_for_process_exit(bootloader, 1000)
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, OSError) as exc:
            AppLogger.error("AppFollow", "停止后台联动守护失败", exc)
        finally:
            stop_event.close()

    @staticmethod
    def stop_all_instances(stop_all: bool = True, force: bool = True) -> None:
        """统一停止主程序与跟随守护进程（CLI 关停与配置清理共用的核心逻辑）。"""
        stop_all_handle = AppFollowService.acquire_stop_all_signal() if stop_all else None
        try:
            live_main = AppFollowService.try_get_live_main_process()
            signaled = AppFollowService.try_signal_named_event(CLOSE_EVENT_NAME)
            if not signaled and AppFollowService.is_main_instance_running():
                # 若主进程刚起步尚未创建命名事件，短暂轮询重发确保收到优雅关闭信号
                wait_sig = time.monotonic() + 1.5
                while time.monotonic() < wait_sig and not signaled and AppFollowService.is_main_instance_running():
                    time.sleep(0.1)
                    signaled = AppFollowService.try_signal_named_event(CLOSE_EVENT_NAME)

            if live_main is None:
                live_main = AppFollowService.try_get_live_main_process()

            # 等待主 GUI 实例优雅保存坐标并退出
            if live_main is not None:
                AppFollowService.wait_for_process_exit(live_main, 3500)
            elif AppFollowService.is_main_instance_running():
                deadline = time.monotonic() + 3.5
                while time.monotonic() < deadline and AppFollowService.is_main_instance_running():
                    time.sleep(0.1)

            if stop_all:
                AppFollowService.stop_follow_watcher(1500)

            if stop_all or force:
                if live_main is not None and live_main.is_running():
                    AppFollowService.kill_process_tree_safe(live_main)
                    AppFollowService.wait_for_process_exit(live_main, 1200)

                for proc in AppFollowService.get_other_pet_processes():
                    AppFollowService.kill_process_tree_safe(proc)

                AppFollowService.clear_all_pid_markers()
                time.sleep(0.2)
        finally:
            if stop_all_handle is not None:
                stop_all_handle.close()

    @classmethod
    def spawn_follow_watcher(cls, from_exit: bool) -> bool:
        """在启用应用联动且未处于全局停机时，拉起与主程序目录解耦的无头后台守护进程。"""
        if cls.is_stop_all_signaled():
            return False
        cmd, cwd = cls.get_follow_watcher_launch_spec(from_exit=from_exit)
        try:
            os.makedirs(cwd, exist_ok=True)
            subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_hidden_kwargs(),
            )
        except Exception as exc:
            AppLogger.warn("AppFollow", f"启动后台冷启动监听进程失败 (cmd={cmd})：{exc}")
            return False
        AppLogger.info("AppFollow", f"已交接至后台冷启动监听进程：cmd={cmd}, cwd={cwd}")
        return True

    @staticmethod
    def try_launch_pet_with_args(
        args: list[str],
        *,
        target_path: Optional[str] = None,
        cwd_override: Optional[str] = None,
    ) -> Optional[subprocess.Popen]:
        if target_path:
            resolved = os.path.abspath(target_path)
            if resolved.lower().endswith(".exe"):
                cmd = [resolved, *args]
            else:
                exe = sys.executable
                candidate = Path(exe).with_name("pythonw.exe")
                if candidate.exists():
                    exe = str(candidate)
                cmd = [exe, resolved, *args]
            cwd = cwd_override or os.path.dirname(resolved)
        else:
            cmd = get_launch_command(args, gui=True)
            cwd = cwd_override or (
                os.path.dirname(os.path.abspath(cmd[0])) if getattr(sys, "frozen", False) else get_app_base_dir()
            )
        try:
            os.makedirs(cwd, exist_ok=True)
            return subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_hidden_kwargs(),
            )
        except Exception as exc:
            AppLogger.warn("FollowWatcher", f"启动桌宠进程失败 ({args})：{exc}")
            return None

    @staticmethod
    def run_follow_watcher(from_exit: bool, target_path: Optional[str] = None) -> int:
        return AppFollowService.run_follow_watcher_core(
            FollowWatcherOptions(from_exit=from_exit, target_path=target_path, poll_interval_ms=1200)
        )

    @staticmethod
    def _resolve_watch_target(target_path: Optional[str]) -> str:
        if target_path and target_path.strip():
            return os.path.abspath(target_path.strip())
        entry_script = os.path.abspath(get_project_entry_script())
        if getattr(sys, "frozen", False):
            return sys.executable
        return entry_script if os.path.exists(entry_script) else os.path.abspath(__file__)

    @classmethod
    def run_follow_watcher_core(cls, options: FollowWatcherOptions) -> int:
        is_standalone = not (options.custom_settings_folder and options.custom_settings_folder.strip())
        should_watch_target = bool(options.target_path and options.target_path.strip()) or is_standalone
        watch_target = cls._resolve_watch_target(options.target_path)
        if (
            cls.is_stop_all_signaled(options.stop_all_event_name)
            or cls.is_main_instance_running(options.main_mutex_name)
        ):
            return 0

        settings_service = SettingsService(options.custom_settings_folder)
        settings = settings_service.load()
        if is_standalone:
            AppLogger.initialize(settings.enable_runtime_log)

        if should_watch_target and watch_target and not os.path.exists(watch_target):
            AppLogger.info("FollowWatcher", f"主程序文件不存在，后台监听自动退出：{watch_target}")
            return 0

        if not cls.any_follow_enabled(settings):
            AppLogger.info("FollowWatcher", "未启用任何跟随应用联动，后台监听退出")
            return 0

        watcher_mutex = Win32Mutex(options.watcher_mutex_name)
        if not watcher_mutex.acquire():
            watcher_mutex.release()
            AppLogger.info("FollowWatcher", "已有后台监听实例在运行，当前进程退出")
            return 0

        stop_event = Win32Event(options.stop_watcher_event_name, manual_reset=True, initial_state=False)
        stop_all_event = Win32Event(options.stop_all_event_name, manual_reset=True, initial_state=False)
        try:
            if is_standalone:
                cls.record_watcher_pid(os.getpid())
            # 停止信号可能在进程初始化期间发出，启动时也必须遵守。
            if wait_any_events([stop_event, stop_all_event], 0) >= 0:
                return 0

            sample_ag = options.sample_antigravity or FollowedAppProbe.sample_antigravity_activity
            sample_gem = options.sample_gemini or FollowedAppProbe.sample_gemini_activity
            sample_cli = options.sample_antigravity_cli or FollowedAppProbe.sample_antigravity_cli_activity

            if not options.from_exit and not cls.is_main_instance_running(options.main_mutex_name):
                already_key = FollowedAppProbe.detect_running_followed_app_key(
                    settings, sample_ag, sample_gem, sample_cli
                )
                if already_key:
                    AppLogger.info("FollowWatcher", f"冷启动监听检测到目标已运行，立即唤醒主程序：{already_key}")
                    if cls._launch_wake_target(options, [f"--woken-by:{already_key}"], stop_event):
                        return 0

            init_ag = sample_ag()
            init_gem = sample_gem()
            init_cli = sample_cli()
            now_utc = datetime.now(timezone.utc)
            was_ag = init_ag.is_running
            was_gem = init_gem.is_running
            was_cli = init_cli.is_running
            known_cli = set(init_cli.active_pids)
            last_cli_start = now_utc if was_cli else None
            known_ag = set(init_ag.active_pids)
            known_gem = set(init_gem.active_pids)
            last_ag_start = now_utc if was_ag else None
            last_gem_start = now_utc if was_gem else None

            settings_path = settings_service.settings_path
            last_settings_mtime = cls._get_mtime_safe(settings_path)
            poll_ms = max(15, options.poll_interval_ms)

            AppLogger.info(
                "FollowWatcher",
                f"后台冷启动监听已启动：fromExit={options.from_exit}, target={watch_target}, "
                f"baseline=(AG:{was_ag}[{len(known_ag)}], CLI:{was_cli}[{len(known_cli)}], Gemini:{was_gem}[{len(known_gem)}]), "
                f"follow=(AG:{settings.follow_antigravity}, CLI:{settings.follow_antigravity_cli}, Gemini:{settings.follow_gemini_app})",
            )

            while wait_any_events([stop_event, stop_all_event], poll_ms) == -1:
                if cls.is_main_instance_running(options.main_mutex_name):
                    break
                if should_watch_target and watch_target and not os.path.exists(watch_target):
                    AppLogger.info("FollowWatcher", "检测到主程序文件已移除，后台监听自动退出以释放占用")
                    break

                cur_mtime = cls._get_mtime_safe(settings_path)
                if cur_mtime != last_settings_mtime:
                    if last_settings_mtime > 0 and cur_mtime == 0:
                        AppLogger.info("FollowWatcher", "检测到 user_config.json 已被移除，后台监听自动退出")
                        break
                    last_settings_mtime = cur_mtime
                    settings = settings_service.load()
                    if is_standalone:
                        AppLogger.set_enabled(settings.enable_runtime_log)
                    if not cls.any_follow_enabled(settings):
                        AppLogger.info("FollowWatcher", "检测到设置已关闭全部跟随联动，后台监听退出")
                        break

                ag_snap = sample_ag()
                gem_snap = sample_gem()
                cli_snap = sample_cli()
                now = datetime.now(timezone.utc)

                ag_started, _, was_ag, known_ag, last_ag_start = FollowedAppProbe.evaluate_transition(
                    ag_snap, was_ag, known_ag, last_ag_start, now
                )
                gem_started, _, was_gem, known_gem, last_gem_start = FollowedAppProbe.evaluate_transition(
                    gem_snap, was_gem, known_gem, last_gem_start, now
                )

                cli_started, _, was_cli, known_cli, last_cli_start = FollowedAppProbe.evaluate_transition(
                    cli_snap, was_cli, known_cli, last_cli_start, now
                )

                wake_cli = settings.follow_antigravity_cli and cli_started
                wake_ag = settings.follow_antigravity and ag_started
                wake_gem = settings.follow_gemini_app and gem_started
                if not wake_ag and not wake_gem and not wake_cli:
                    continue

                if cls.is_main_instance_running(options.main_mutex_name):
                    continue

                app_key = "antigravity" if wake_ag else ("gemini" if wake_gem else "antigravity-cli")
                AppLogger.info("FollowWatcher", f"检测到冷启动目标应用：{app_key}，正在唤醒桌宠主程序")
                if cls._launch_wake_target(options, [f"--woken-by:{app_key}"], stop_event):
                    return 0

            AppLogger.info("FollowWatcher", "后台冷启动监听已收到停止信号并退出")
            return 0
        finally:
            stop_event.close()
            stop_all_event.close()
            if is_standalone:
                cls.clear_watcher_pid(os.getpid())
            watcher_mutex.release()

    @classmethod
    def _launch_wake_target(
        cls, options: FollowWatcherOptions, args: list[str], stop_event: Win32Event
    ) -> bool:
        if options.custom_wake_launcher is not None:
            options.custom_wake_launcher(" ".join(args))
            return wait_any_events([stop_event], 0) == 0

        proc = cls.try_launch_pet_with_args(args, target_path=options.target_path)
        if proc is None:
            return False

        for _ in range(60):
            if wait_any_events([stop_event], 120) == 0 or cls.is_stop_all_signaled(options.stop_all_event_name):
                return True
            if cls.is_main_instance_running(options.main_mutex_name):
                return True
            if proc.poll() is not None:
                return wait_any_events([stop_event], 0) == 0 or cls.is_stop_all_signaled(options.stop_all_event_name)
        return wait_any_events([stop_event], 0) == 0 or cls.is_stop_all_signaled(options.stop_all_event_name)

    @staticmethod
    def _write_pid_marker(filename: str, pid: int) -> None:
        try:
            user_dir = str(get_user_config_dir())
            os.makedirs(user_dir, exist_ok=True)
            create_time = int(psutil.Process(pid).create_time())
            with open(_get_pid_marker_path(filename), "w", encoding="utf-8") as fp:
                fp.write(f"{pid}|{create_time}")
        except Exception:
            pass

    @staticmethod
    def _clear_pid_marker(filename: str, pid: int) -> None:
        path = _get_pid_marker_path(filename)
        try:
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as fp:
                raw = fp.read().strip()
            pid_str = raw.split("|", 1)[0]
            if int(pid_str) == pid:
                os.remove(path)
        except Exception:
            pass

    @staticmethod
    def _get_recorded_live_process(filename: str) -> Optional[psutil.Process]:
        path = _get_pid_marker_path(filename)
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as fp:
                parts = fp.read().strip().split("|")
            pid = int(parts[0])
            if pid <= 0:
                raise ValueError("Invalid pid")
            proc = psutil.Process(pid)
            if not proc.is_running() or not _is_pet_process(proc):
                raise ValueError("Process mismatch")
            if filename == _WATCHER_PID_FILE and "--follow-watcher" not in (
                arg.lower() for arg in proc.cmdline()
            ):
                raise ValueError("Process is not a follow watcher")
            if len(parts) >= 2:
                rec_ts = int(parts[1])
                if abs(int(proc.create_time()) - rec_ts) > 5:
                    raise ValueError("PID reused")
            return proc
        except Exception:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            return None

    @staticmethod
    def _get_mtime_safe(path: str) -> float:
        try:
            return os.path.getmtime(path) if os.path.exists(path) else 0.0
        except Exception:
            return 0.0

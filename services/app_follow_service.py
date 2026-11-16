"""应用跟随联动待机引擎、跨进程唤醒/停机信令及冷启动后台守护（--follow-watcher）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
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
_MAIN_PID_FILE = "main.pid"
_WATCHER_PID_FILE = "watcher.pid"


@dataclass
class FollowWatcherOptions:
    from_exit: bool = False
    custom_settings_folder: Optional[str] = None
    main_mutex_name: str = MAIN_MUTEX_NAME
    watcher_mutex_name: str = WATCHER_MUTEX_NAME
    stop_watcher_event_name: str = STOP_WATCHER_EVENT_NAME
    stop_all_event_name: str = STOP_ALL_EVENT_NAME
    poll_interval_ms: int = 1200
    sample_antigravity: Optional[Callable[[], AppActivitySnapshot]] = None
    sample_gemini: Optional[Callable[[], AppActivitySnapshot]] = None
    custom_wake_launcher: Optional[Callable[[str], bool]] = None


def _get_pid_marker_path(filename: str) -> str:
    return os.path.join(str(get_user_config_dir()), filename)


def _is_pet_process(proc: psutil.Process) -> bool:
    """判断进程是否属于本桌宠（同时兼容打包后的 GeminiDesktopPet.exe 与源码态 python main.py）。"""
    try:
        name = (proc.name() or "").lower()
        if name == "geminidesktoppet.exe" or name == "geminidesktoppet":
            return True
        if name.startswith("python"):
            entry_norm = os.path.normcase(os.path.abspath(get_project_entry_script()))
            for arg in proc.cmdline() or ():
                if os.path.normcase(os.path.abspath(arg)) == entry_norm or os.path.basename(arg).lower() == "main.py":
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
        return bool(settings.follow_antigravity or settings.follow_gemini_app)

    @classmethod
    def get_follow_watcher_shortcut_path(cls) -> Path:
        return get_windows_startup_dir() / FOLLOW_STARTUP_SHORTCUT_NAME

    @classmethod
    def is_follow_watcher_run_enabled(cls) -> bool:
        return is_startup_entry_enabled(FOLLOW_STARTUP_SHORTCUT_NAME)

    @classmethod
    def set_follow_watcher_run(cls, enable: bool) -> bool:
        icon_candidate = Path(get_bundled_resource_dir()) / "Assets" / "AppIcon.ico"
        return set_startup_entry_enabled(
            FOLLOW_STARTUP_SHORTCUT_NAME,
            enable,
            args=["--follow-watcher"],
            description="GeminiDesktopPet 后台跟随联动待机",
            icon_path=str(icon_candidate) if icon_candidate.is_file() else None,
            working_dir=get_app_base_dir(),
        )

    def is_follow_logon_enabled(self) -> bool:
        return self.is_follow_watcher_run_enabled() or SettingsService.is_startup_enabled()

    def sync_and_apply(self, settings: PetSettings) -> None:
        enabled = self.any_follow_enabled(settings)
        self.set_follow_watcher_run(enabled)
        if enabled:
            if not self.is_stop_all_signaled():
                self._replace_mismatched_watcher_if_needed()
                if not self.is_follow_watcher_running():
                    self.spawn_follow_watcher(from_exit=True)
        else:
            self.signal_stop_follow_watcher()
            self.wait_and_stop_live_watcher(500)
        AppLogger.info(
            "AppFollow",
            f"同步应用联动状态：Antigravity={settings.follow_antigravity}, GeminiApp={settings.follow_gemini_app}, StartupShortcut={enabled}",
        )

    def disable_follow(self) -> None:
        self.set_follow_watcher_run(False)
        self.signal_stop_follow_watcher()
        self.wait_and_stop_live_watcher(500)
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
    def signal_stop_follow_watcher() -> bool:
        return Win32Event.open_and_set(STOP_WATCHER_EVENT_NAME)

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
        except psutil.TimeoutExpired:
            return False
        except Exception:
            return True

    @staticmethod
    def wait_and_stop_live_watcher(timeout_ms: int = 800) -> None:
        watcher = AppFollowService.try_get_live_watcher_process()
        if watcher is None:
            return
        if not AppFollowService.wait_for_process_exit(watcher, timeout_ms):
            AppFollowService.kill_process_tree_safe(watcher)
            AppFollowService.wait_for_process_exit(watcher, 1000)

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
                AppFollowService.signal_stop_follow_watcher()
                AppFollowService.wait_and_stop_live_watcher(1500)

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

    @staticmethod
    def spawn_follow_watcher(from_exit: bool) -> bool:
        if AppFollowService.is_stop_all_signaled():
            return False
        args = ["--follow-watcher", "--from-exit"] if from_exit else ["--follow-watcher"]
        proc = AppFollowService.try_launch_pet_with_args(args)
        if proc is None:
            AppLogger.warn("AppFollow", f"启动后台冷启动监听进程失败：args={' '.join(args)}")
            return False
        AppLogger.info("AppFollow", f"已交接至后台冷启动监听进程：args={' '.join(args)}")
        return True

    @staticmethod
    def try_launch_pet_with_args(args: list[str]) -> Optional[subprocess.Popen]:
        cmd = get_launch_command(args, gui=True)
        try:
            cwd = os.path.dirname(os.path.abspath(cmd[0])) if getattr(sys, "frozen", False) else get_app_base_dir()
            return subprocess.Popen(
                cmd,
                cwd=cwd,
                **subprocess_hidden_kwargs(),
            )
        except Exception as exc:
            AppLogger.warn("FollowWatcher", f"启动桌宠进程失败 ({args})：{exc}")
            return None

    @staticmethod
    def run_follow_watcher(from_exit: bool) -> int:
        return AppFollowService.run_follow_watcher_core(
            FollowWatcherOptions(from_exit=from_exit, poll_interval_ms=1200)
        )

    @classmethod
    def run_follow_watcher_core(cls, options: FollowWatcherOptions) -> int:
        is_standalone = not (options.custom_settings_folder and options.custom_settings_folder.strip())
        if cls.is_stop_all_signaled(options.stop_all_event_name):
            return 0

        settings_service = SettingsService(options.custom_settings_folder)
        settings = settings_service.load()
        if is_standalone:
            AppLogger.initialize(settings.enable_runtime_log)

        if not cls.any_follow_enabled(settings):
            AppLogger.info("FollowWatcher", "未启用任何跟随应用联动，后台监听退出")
            return 0

        watcher_mutex = Win32Mutex(options.watcher_mutex_name)
        if not watcher_mutex.acquire():
            AppLogger.info("FollowWatcher", "已有后台监听实例在运行，当前进程退出")
            return 0

        if is_standalone:
            cls.record_watcher_pid(os.getpid())

        stop_event = Win32Event(options.stop_watcher_event_name, manual_reset=True, initial_state=False)
        stop_all_event = Win32Event(options.stop_all_event_name, manual_reset=True, initial_state=False)
        try:
            # 重置历史残余的 stopWatcher 信号（若非处于 stopAll 中）
            if wait_any_events([stop_all_event], 0) == 0:
                AppLogger.info("FollowWatcher", "检测到强制全部停止信号，后台监听直接退出")
                return 0

            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).ResetEvent(stop_event.handle)

            sample_ag = options.sample_antigravity or FollowedAppProbe.sample_antigravity_activity
            sample_gem = options.sample_gemini or FollowedAppProbe.sample_gemini_activity

            if not options.from_exit and not cls.is_main_instance_running(options.main_mutex_name):
                already_key = FollowedAppProbe.detect_running_followed_app_key(
                    settings, sample_ag, sample_gem
                )
                if already_key:
                    AppLogger.info("FollowWatcher", f"冷启动监听检测到目标已运行，立即唤醒主程序：{already_key}")
                    if cls._launch_wake_target(options, [f"--woken-by:{already_key}"], stop_event):
                        return 0

            init_ag = sample_ag()
            init_gem = sample_gem()
            now_utc = datetime.now(timezone.utc)
            was_ag = init_ag.is_running
            was_gem = init_gem.is_running
            known_ag = set(init_ag.active_pids)
            known_gem = set(init_gem.active_pids)
            last_ag_start = now_utc if was_ag else None
            last_gem_start = now_utc if was_gem else None

            settings_path = settings_service.settings_path
            last_settings_mtime = cls._get_mtime_safe(settings_path)
            poll_ms = max(15, options.poll_interval_ms)
            entry_script = os.path.abspath(get_project_entry_script())
            watch_target = (
                sys.executable
                if getattr(sys, "frozen", False)
                else (entry_script if os.path.exists(entry_script) else os.path.abspath(__file__))
            )

            AppLogger.info(
                "FollowWatcher",
                f"后台冷启动监听已启动：fromExit={options.from_exit}, baseline=(AG:{was_ag}[{len(known_ag)}], Gemini:{was_gem}[{len(known_gem)}]), follow=(AG:{settings.follow_antigravity}, Gemini:{settings.follow_gemini_app})",
            )

            while wait_any_events([stop_event, stop_all_event], poll_ms) == -1:
                if is_standalone and watch_target and not os.path.exists(watch_target):
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
                now = datetime.now(timezone.utc)

                ag_started, _, was_ag, known_ag, last_ag_start = FollowedAppProbe.evaluate_transition(
                    ag_snap, was_ag, known_ag, last_ag_start, now
                )
                gem_started, _, was_gem, known_gem, last_gem_start = FollowedAppProbe.evaluate_transition(
                    gem_snap, was_gem, known_gem, last_gem_start, now
                )

                wake_ag = settings.follow_antigravity and ag_started
                wake_gem = settings.follow_gemini_app and gem_started
                if not wake_ag and not wake_gem:
                    continue

                if cls.is_main_instance_running(options.main_mutex_name):
                    continue

                app_key = "antigravity" if wake_ag else "gemini"
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

        proc = cls.try_launch_pet_with_args(args)
        if proc is None:
            return False

        for _ in range(60):
            if wait_any_events([stop_event], 120) == 0 or cls.is_stop_all_signaled():
                return True
            if cls.is_main_instance_running(options.main_mutex_name):
                return False
            if proc.poll() is not None:
                return wait_any_events([stop_event], 0) == 0 or cls.is_stop_all_signaled()
        return wait_any_events([stop_event], 0) == 0 or cls.is_stop_all_signaled()

    @classmethod
    def _replace_mismatched_watcher_if_needed(cls) -> None:
        live = cls.try_get_live_watcher_process()
        if live is None:
            return
        try:
            current_dir = os.path.normcase(os.path.abspath(get_app_base_dir()))
            proc_exe = os.path.normcase(os.path.abspath(live.exe()))
            if getattr(sys, "frozen", False):
                if os.path.normcase(os.path.abspath(sys.executable)) == proc_exe:
                    return
            else:
                entry = os.path.normcase(os.path.abspath(get_project_entry_script()))
                if any(os.path.normcase(os.path.abspath(a)) == entry for a in (live.cmdline() or ())):
                    return
                if current_dir in proc_exe:
                    return
            AppLogger.info("AppFollow", f"检测到旧路径后台监听进程 (PID={live.pid})，正在替换为当前实例")
            cls.signal_stop_follow_watcher()
            if not cls.wait_for_process_exit(live, 1200):
                cls.kill_process_tree_safe(live)
                cls.wait_for_process_exit(live, 1000)
        except Exception:
            pass

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

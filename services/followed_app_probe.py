"""纯 Win32/psutil 进程探测器（无任何 Qt 依赖），用于 --follow-watcher 与主程序探测联动软件。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import psutil

from models.pet_settings import PetSettings

ANTIGRAVITY_PROCESS_NAME = "Antigravity"
GEMINI_PROCESS_NAME = "Gemini"
MIN_ACTIVE_ELECTRON_THREAD_COUNT = 22
MIN_PRIMARY_BROWSER_THREAD_COUNT = 38

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GW_OWNER = 4
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _snapshot_process_entries() -> list[tuple[int, str, int]]:
    """使用 Win32 Toolhelp 快照一次性读取全部进程的 PID、镜像名与线程数，避免 psutil O(N^2) 阻塞。"""
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed")
    entries: list[tuple[int, str, int]] = []
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if _kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                entries.append((int(pe.th32ProcessID), pe.szExeFile, int(pe.cntThreads)))
                if not _kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return entries


@dataclass(frozen=True)
class ProcessProbeInfo:
    pid: int
    has_main_window: bool
    thread_count: int


@dataclass
class AppActivitySnapshot:
    is_running: bool
    active_pids: set[int]
    primary_pids: set[int]


def _collect_visible_main_window_pids() -> set[int]:
    """枚举当前存在可见且无宿主顶层主窗口的进程 PID。"""
    visible_pids: set[int] = set()

    def _callback(hwnd: int, _lparam: int) -> bool:
        try:
            if _user32.IsWindowVisible(hwnd) and _user32.GetWindow(hwnd, _GW_OWNER) == 0:
                pid = wintypes.DWORD(0)
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    visible_pids.add(int(pid.value))
        except Exception:
            pass
        return True

    try:
        cb = _WNDENUMPROC(_callback)
        _user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return visible_pids


class FollowedAppProbe:
    """无 UI 依赖的目标进程探测与启停跳变状态机。"""

    @staticmethod
    def sample_antigravity_activity() -> AppActivitySnapshot:
        return FollowedAppProbe.sample_app_activity(ANTIGRAVITY_PROCESS_NAME)

    @staticmethod
    def sample_gemini_activity() -> AppActivitySnapshot:
        return FollowedAppProbe.sample_app_activity(GEMINI_PROCESS_NAME)

    @staticmethod
    def sample_antigravity_cli_activity() -> AppActivitySnapshot:
        # CLI 没有独立主窗口且线程数较少，不能套用 Electron 的活跃进程过滤。
        try:
            pids = {
                pid for pid, name, _ in _snapshot_process_entries()
                if name.lower() in {"agy", "agy.exe"}
            }
        except Exception:
            pids = set()
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        if (proc.info.get("name") or "").lower() in {"agy", "agy.exe"}:
                            pids.add(int(proc.info["pid"]))
                    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
                        continue
            except (psutil.Error, OSError):
                pass
        # 额度查询也会短暂启动 agy；按命令识别，避免主程序和后台守护误唤醒。
        # 不暂停整个监听，因此查询期间用户新开的交互式 CLI 仍可正常联动。
        pids = {pid for pid in pids if not FollowedAppProbe._is_cli_quota_query(pid)}
        return AppActivitySnapshot(bool(pids), pids, set(pids))

    @staticmethod
    def sample_all_activities(
        *,
        check_antigravity: bool = True,
        check_gemini: bool = True,
        check_cli: bool = True,
    ) -> tuple[AppActivitySnapshot, AppActivitySnapshot, AppActivitySnapshot]:
        """合并单次进程快照与单次窗口枚举，同时采样三个联动目标，避免重复系统调用。"""
        empty = AppActivitySnapshot(False, set(), set())
        if not (check_antigravity or check_gemini or check_cli):
            return (
                AppActivitySnapshot(False, set(), set()),
                AppActivitySnapshot(False, set(), set()),
                AppActivitySnapshot(False, set(), set()),
            )

        try:
            entries = _snapshot_process_entries()
        except Exception:
            return (
                FollowedAppProbe.sample_antigravity_activity() if check_antigravity else AppActivitySnapshot(False, set(), set()),
                FollowedAppProbe.sample_gemini_activity() if check_gemini else AppActivitySnapshot(False, set(), set()),
                FollowedAppProbe.sample_antigravity_cli_activity() if check_cli else AppActivitySnapshot(False, set(), set()),
            )

        visible_pids = (
            _collect_visible_main_window_pids()
            if (check_antigravity or check_gemini)
            else set()
        )
        ag_target = (f"{ANTIGRAVITY_PROCESS_NAME.lower()}.exe", ANTIGRAVITY_PROCESS_NAME.lower())
        gem_target = (f"{GEMINI_PROCESS_NAME.lower()}.exe", GEMINI_PROCESS_NAME.lower())

        ag_probes: list[ProcessProbeInfo] = []
        gem_probes: list[ProcessProbeInfo] = []
        cli_pids: set[int] = set()

        for pid, exe_name, threads in entries:
            pname = exe_name.lower()
            if check_antigravity and pname in ag_target:
                ag_probes.append(ProcessProbeInfo(pid, pid in visible_pids, threads))
            elif check_gemini and pname in gem_target:
                gem_probes.append(ProcessProbeInfo(pid, pid in visible_pids, threads))
            elif check_cli and pname in {"agy", "agy.exe"}:
                cli_pids.add(pid)

        ag_snap = (
            FollowedAppProbe.build_snapshot_from_probes(ag_probes)
            if check_antigravity
            else AppActivitySnapshot(False, set(), set())
        )
        gem_snap = (
            FollowedAppProbe.build_snapshot_from_probes(gem_probes)
            if check_gemini
            else AppActivitySnapshot(False, set(), set())
        )
        if check_cli:
            cli_pids = {pid for pid in cli_pids if not FollowedAppProbe._is_cli_quota_query(pid)}
            cli_snap = AppActivitySnapshot(bool(cli_pids), cli_pids, set(cli_pids))
        else:
            cli_snap = AppActivitySnapshot(False, set(), set())
        del empty
        return ag_snap, gem_snap, cli_snap

    @staticmethod
    def _is_cli_quota_query(pid: int) -> bool:
        try:
            args = psutil.Process(pid).cmdline()[1:]
        except (psutil.Error, OSError):
            return False
        return any(
            flag in {"-p", "--print", "--prompt"} and value in {"/usage", "/quota"}
            for flag, value in zip(args, args[1:])
        )

    @staticmethod
    def detect_running_followed_app_key(
        settings: PetSettings,
        sample_ag: Optional[Callable[[], AppActivitySnapshot]] = None,
        sample_gemini: Optional[Callable[[], AppActivitySnapshot]] = None,
        sample_cli: Optional[Callable[[], AppActivitySnapshot]] = None,
    ) -> Optional[str]:
        if settings.follow_antigravity:
            snap = (sample_ag or FollowedAppProbe.sample_antigravity_activity)()
            if snap.is_running:
                return "antigravity"
        if settings.follow_gemini_app:
            snap = (sample_gemini or FollowedAppProbe.sample_gemini_activity)()
            if snap.is_running:
                return "gemini"
        if settings.follow_antigravity_cli:
            snap = (sample_cli or FollowedAppProbe.sample_antigravity_cli_activity)()
            if snap.is_running:
                return "antigravity-cli"
        return None

    @staticmethod
    def classify_process_candidate(has_main_window: bool, thread_count: int) -> tuple[bool, bool]:
        # 过滤 Electron 关闭后残留的低线程孤儿 Node/MCP worker，避免误判为应用在运行
        is_active = has_main_window or thread_count >= MIN_ACTIVE_ELECTRON_THREAD_COUNT
        is_primary = is_active and (has_main_window or thread_count >= MIN_PRIMARY_BROWSER_THREAD_COUNT)
        return is_active, is_primary

    @staticmethod
    def build_snapshot_from_probes(probes: Iterable[ProcessProbeInfo]) -> AppActivitySnapshot:
        active_pids: set[int] = set()
        primary_pids: set[int] = set()
        for probe in probes:
            is_active, is_primary = FollowedAppProbe.classify_process_candidate(
                probe.has_main_window, probe.thread_count
            )
            if is_active:
                active_pids.add(probe.pid)
                if is_primary:
                    primary_pids.add(probe.pid)
        return AppActivitySnapshot(
            is_running=len(active_pids) > 0,
            active_pids=active_pids,
            primary_pids=primary_pids,
        )

    @staticmethod
    def sample_app_activity(process_name: str) -> AppActivitySnapshot:
        target_exe = f"{process_name.lower()}.exe"
        target_stem = process_name.lower()
        visible_pids = _collect_visible_main_window_pids()
        probes: list[ProcessProbeInfo] = []

        try:
            for pid, exe_name, threads in _snapshot_process_entries():
                pname = exe_name.lower()
                if pname == target_exe or pname == target_stem:
                    probes.append(ProcessProbeInfo(pid, pid in visible_pids, threads))
            return FollowedAppProbe.build_snapshot_from_probes(probes)
        except Exception:
            pass

        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    info = proc.info
                    pname = (info.get("name") or "").lower()
                    if pname != target_exe and pname != target_stem:
                        continue
                    pid = int(info["pid"])
                    threads = int(proc.num_threads() or 0)
                    probes.append(ProcessProbeInfo(pid, pid in visible_pids, threads))
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
                    continue
        except Exception:
            pass

        return FollowedAppProbe.build_snapshot_from_probes(probes)

    @staticmethod
    def evaluate_transition(
        current: AppActivitySnapshot,
        was_running: bool,
        known_pids: set[int],
        last_start_utc: Optional[datetime],
        now_utc: Optional[datetime] = None,
    ) -> tuple[bool, bool, bool, set[int], Optional[datetime]]:
        """计算启停跳变，防止 Electron 多进程拉起与 GPU/Renderer 子进程波动造成重复唤醒。"""
        now = now_utc or datetime.now(timezone.utc)
        known = known_pids & current.active_pids

        has_known_primary_alive = bool(current.primary_pids & known)
        has_brand_new_primary = any(pid not in known for pid in current.primary_pids)

        cooldown_elapsed = (
            True
            if last_start_utc is None
            else (now - last_start_utc).total_seconds() >= 6.0
        )
        started = (current.is_running and not was_running) or (
            has_brand_new_primary and (not has_known_primary_alive or cooldown_elapsed)
        )
        exited = (not current.is_running) and was_running

        new_last_start = last_start_utc
        if started:
            new_last_start = now
            new_known = set(current.active_pids)
        elif current.primary_pids:
            new_known = known | current.active_pids
        elif not current.is_running:
            new_known = set()
        else:
            new_known = known

        return started, exited, current.is_running, new_known, new_last_start

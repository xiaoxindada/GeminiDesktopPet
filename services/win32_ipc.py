"""Windows 原生 IPC、启动目录快捷方式、窗口样式与启动命令行辅助工具。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Optional
import uuid

MAIN_MUTEX_NAME = "GeminiDesktopPet_SingleInstance_Mutex"
WATCHER_MUTEX_NAME = "GeminiDesktopPet_FollowWatcher_Mutex"
WAKE_EVENT_NAME = "GeminiDesktopPet_WakeSignal_Event"
WAKE_AG_CLI_EVENT_NAME = "GeminiDesktopPet_WakeAgCliSignal_Event"
WAKE_AG_EVENT_NAME = "GeminiDesktopPet_WakeAgSignal_Event"
WAKE_GEMINI_EVENT_NAME = "GeminiDesktopPet_WakeGeminiSignal_Event"
CLOSE_EVENT_NAME = "GeminiDesktopPet_CloseSignal_Event"
STOP_WATCHER_EVENT_NAME = "GeminiDesktopPet_StopFollowWatcher_Event"
STOP_ALL_EVENT_NAME = "GeminiDesktopPet_StopAll_Event"

_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF

_GWL_EXSTYLE = -20

_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_HWND_BOTTOM = 1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_SW_SHOW = 5
_SW_RESTORE = 9

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_kernel32.ReleaseMutex.restype = wintypes.BOOL

_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD

_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetActiveWindow.argtypes = [wintypes.HWND]
_user32.SetActiveWindow.restype = wintypes.HWND
_shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
_shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
_shell32.SHGetFolderPathW.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
]
_shell32.SHGetFolderPathW.restype = ctypes.HRESULT


class Win32Mutex:
    """Windows 命名互斥体封装，用于单实例与守护进程锁。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: Optional[wintypes.HANDLE] = None
        self.created_new = False

    def acquire(self) -> bool:
        handle = _kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            return False
        err = ctypes.get_last_error()
        self._handle = handle
        self.created_new = err != _ERROR_ALREADY_EXISTS
        return self.created_new

    @staticmethod
    def exists(name: str) -> bool:
        handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
        if handle:
            _kernel32.CloseHandle(handle)
            return True
        return False

    def release(self) -> None:
        if self._handle:
            if self.created_new:
                _kernel32.ReleaseMutex(self._handle)
            _kernel32.CloseHandle(self._handle)
            self._handle = None
            self.created_new = False


class Win32Event:
    """Windows 命名事件封装，用于跨进程唤醒与退出信号通知。"""

    def __init__(self, name: str, manual_reset: bool = False, initial_state: bool = False) -> None:
        self.name = name
        self.handle: Optional[wintypes.HANDLE] = _kernel32.CreateEventW(
            None, manual_reset, initial_state, name
        )

    @staticmethod
    def open_and_set(name: str) -> bool:
        handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(_kernel32.SetEvent(handle))
        finally:
            _kernel32.CloseHandle(handle)

    def set(self) -> bool:
        if not self.handle:
            return False
        return bool(_kernel32.SetEvent(self.handle))

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def wait_any_events(events: Iterable[Win32Event], timeout_ms: int) -> int:
    """等待任意事件触发，返回索引；超时返回 -1。"""
    handles = [ev.handle for ev in events if ev.handle]
    if not handles:
        return -1
    arr = (wintypes.HANDLE * len(handles))(*handles)
    res = _kernel32.WaitForMultipleObjects(len(handles), arr, False, timeout_ms)
    if res == _WAIT_TIMEOUT or res == _WAIT_FAILED:
        return -1
    idx = res - _WAIT_OBJECT_0
    if 0 <= idx < len(handles):
        return idx
    return -1


def ensure_app_user_model_id(app_id: str = "GeminiDesktopPet") -> None:
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def bring_window_to_front(hwnd: int) -> None:
    """将普通设置/详情窗口还原、提到前层并请求前台焦点。"""
    if not hwnd or sys.platform != "win32":
        return
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        else:
            _user32.ShowWindow(hwnd, _SW_SHOW)

        # 通过短暂置顶脉冲 + 临时附加前台线程输入队列，绕过托盘/Tool 窗口弹出后的 Z 序遮挡与聚焦限制
        was_topmost = bool(_user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) & 0x00000008)
        fg_hwnd = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        cur_tid = _kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            _user32.SetWindowPos(
                hwnd,
                _HWND_TOPMOST,
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW,
            )
            if not was_topmost:
                _user32.SetWindowPos(
                    hwnd,
                    _HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW,
                )
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        pass


def send_window_to_bottom(hwnd: int) -> None:
    if not hwnd:
        return
    try:
        _user32.SetWindowPos(
            hwnd, _HWND_BOTTOM, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
        )
    except Exception:
        pass


def get_project_entry_script() -> str:
    return str(Path(__file__).resolve().parents[1] / "main.py")


def get_launch_command(args: Iterable[str] = (), *, gui: bool = True) -> list[str]:
    """构建自举命令行：打包态返回 exe 路径；源码态优先 main.py，否则回退 python -m cli。"""
    arg_list = list(args)
    if getattr(sys, "frozen", False):
        return [sys.executable, *arg_list]

    exe = sys.executable
    if gui:
        candidate = Path(exe).with_name("pythonw.exe")
        if candidate.exists():
            exe = str(candidate)

    entry_script = Path(get_project_entry_script())
    if entry_script.is_file():
        return [exe, str(entry_script), *arg_list]
    return [exe, "-m", "cli", *arg_list]


def subprocess_no_window_flags() -> int:
    return 0x08000000 if os.name == "nt" else 0


def subprocess_hidden_kwargs() -> dict[str, Any]:
    """仅设置无窗口创建标志（CREATE_NO_WINDOW），避免 STARTUPINFO(SW_HIDE) 影响子进程初始化或校验。"""
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess_no_window_flags()}


_CSIDL_STARTUP = 0x0007
_COINIT_APARTMENTTHREADED = 0x2


class _COMGUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(guid_text: str) -> _COMGUID:
    u = uuid.UUID(guid_text)
    return _COMGUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def get_windows_startup_dir() -> Path:
    """获取当前用户的 Windows 启动文件夹路径（shell:startup）。"""
    if os.name == "nt":
        try:
            buf = ctypes.create_unicode_buffer(260)
            if _shell32.SHGetFolderPathW(0, _CSIDL_STARTUP, 0, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def create_windows_shortcut(
    shortcut_path: Path,
    target_path: str,
    arguments: str = "",
    working_dir: str = "",
    description: str = "",
    icon_path: Optional[str] = None,
) -> bool:
    """通过原生 COM IShellLinkW 创建 Windows 快捷方式（.lnk），免注册表写入且无黑框脚本。"""
    if os.name != "nt":
        return False

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_COMGUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_COMGUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long

    # 仅在当前线程由本函数初始化 COM（S_OK / S_FALSE）时配对释放
    hr_init = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    should_uninit = hr_init in (0, 1)
    p_shell_link = ctypes.c_void_p()
    p_persist_file = ctypes.c_void_p()
    rel_sl = None
    rel_pf = None
    try:
        clsid_sl = _make_guid("00021401-0000-0000-C000-000000000046")
        iid_isl = _make_guid("000214F9-0000-0000-C000-000000000046")
        iid_ipf = _make_guid("0000010B-0000-0000-C000-000000000046")

        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_sl), None, 1, ctypes.byref(iid_isl), ctypes.byref(p_shell_link)
        )
        if hr < 0 or not p_shell_link.value:
            return False

        # 通过 IShellLinkW 与 IPersistFile 的标准 COM 虚函数表索引直接绑定方法：
        # IShellLinkW: 0=QueryInterface, 2=Release, 7=SetDescription, 9=SetWorkingDirectory,
        #              11=SetArguments, 17=SetIconLocation, 20=SetPath
        vt_sl = ctypes.cast(
            ctypes.cast(p_shell_link, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        qi = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_COMGUID), ctypes.POINTER(ctypes.c_void_p)
        )(vt_sl[0])
        rel_sl = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt_sl[2])
        set_desc = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vt_sl[7])
        set_wd = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vt_sl[9])
        set_args = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vt_sl[11])
        set_icon = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int)(
            vt_sl[17]
        )
        set_path = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vt_sl[20])

        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
        set_path(p_shell_link, target_path)
        if arguments:
            set_args(p_shell_link, arguments)
        if working_dir:
            set_wd(p_shell_link, working_dir)
        if description:
            set_desc(p_shell_link, description)
        if icon_path:
            set_icon(p_shell_link, icon_path, 0)

        if qi(p_shell_link, ctypes.byref(iid_ipf), ctypes.byref(p_persist_file)) < 0:
            return False
        # IPersistFile: 2=Release, 6=Save
        vt_pf = ctypes.cast(
            ctypes.cast(p_persist_file, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        rel_pf = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt_pf[2])
        save_pf = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.BOOL)(
            vt_pf[6]
        )
        return save_pf(p_persist_file, str(shortcut_path), True) >= 0
    except Exception:
        return False
    finally:
        if p_persist_file.value and rel_pf is not None:
            rel_pf(p_persist_file)
        if p_shell_link.value and rel_sl is not None:
            rel_sl(p_shell_link)
        if should_uninit:
            ole32.CoUninitialize()


def is_startup_entry_enabled(shortcut_name: str) -> bool:
    shortcut_path = get_windows_startup_dir() / shortcut_name
    return shortcut_path.is_file()


def set_startup_entry_enabled(
    shortcut_name: str,
    enable: bool,
    args: Iterable[str] = (),
    *,
    description: str = "",
    icon_path: Optional[str] = None,
    working_dir: Optional[str] = None,
) -> bool:
    """启用或取消用户启动目录快捷方式（.lnk）。"""
    shortcut_path = get_windows_startup_dir() / shortcut_name
    if enable:
        cmd = get_launch_command(args, gui=True)
        target = cmd[0]
        arg_str = subprocess.list2cmdline(cmd[1:]) if len(cmd) > 1 else ""
        wd = working_dir or str(Path(target).resolve().parent)
        return create_windows_shortcut(
            shortcut_path,
            target_path=target,
            arguments=arg_str,
            working_dir=wd,
            description=description,
            icon_path=icon_path,
        )

    try:
        if shortcut_path.exists():
            shortcut_path.unlink()
        return True
    except OSError:
        return False

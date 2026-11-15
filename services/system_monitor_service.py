"""通过 Win32 API 采样本机 CPU、内存与电池状态的系统状态监控服务。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetSystemTimes.argtypes = [
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
_kernel32.GetSystemTimes.restype = wintypes.BOOL
_kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
_kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
_kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SystemPowerStatus)]
_kernel32.GetSystemPowerStatus.restype = wintypes.BOOL


def _ft_to_int(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


@dataclass(frozen=True)
class SystemMetrics:
    cpu_percent: float
    ram_percent: float
    battery_percent: int
    is_charging: bool
    has_battery: bool


class SystemMonitorService:
    """低开销采样本机资源占用，触发疲劳/忙碌等桌宠表情反馈。"""

    def __init__(self) -> None:
        self._has_baseline = False
        self._prev_idle = 0
        self._prev_kernel = 0
        self._prev_user = 0
        # 初始化 CPU 基线，避免首次采样虚高
        self._sample_cpu_percent()

    def sample(self) -> SystemMetrics:
        cpu = max(0.0, min(100.0, self._sample_cpu_percent()))
        ram = self._sample_ram_percent()
        bat_pct = 100
        charging = True
        has_bat = False

        try:
            ps = _SystemPowerStatus()
            if _kernel32.GetSystemPowerStatus(ctypes.byref(ps)):
                if ps.BatteryFlag != 128 and ps.BatteryFlag != 255:
                    has_bat = True
                    pct = int(ps.BatteryLifePercent)
                    bat_pct = 100 if pct > 100 else max(0, pct)
                    charging = ps.ACLineStatus == 1
        except Exception:
            pass

        return SystemMetrics(
            cpu_percent=cpu,
            ram_percent=ram,
            battery_percent=bat_pct,
            is_charging=charging,
            has_battery=has_bat,
        )

    def _sample_cpu_percent(self) -> float:
        try:
            idle_ft, kernel_ft, user_ft = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not _kernel32.GetSystemTimes(
                ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)
            ):
                return 0.0
            idle = _ft_to_int(idle_ft)
            kernel = _ft_to_int(kernel_ft)
            user = _ft_to_int(user_ft)

            if not self._has_baseline:
                self._prev_idle, self._prev_kernel, self._prev_user = idle, kernel, user
                self._has_baseline = True
                return 0.0

            d_idle = idle - self._prev_idle
            d_kernel = kernel - self._prev_kernel
            d_user = user - self._prev_user
            self._prev_idle, self._prev_kernel, self._prev_user = idle, kernel, user

            total = d_kernel + d_user
            if total <= 0:
                return 0.0
            busy = max(0, total - d_idle)
            return (busy / total) * 100.0
        except Exception:
            return 0.0

    @staticmethod
    def _sample_ram_percent() -> float:
        try:
            ms = _MemoryStatusEx()
            ms.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if _kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)) and ms.ullTotalPhys > 0:
                return float(ms.dwMemoryLoad)
        except Exception:
            pass
        return 0.0

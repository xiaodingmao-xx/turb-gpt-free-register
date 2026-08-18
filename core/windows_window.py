# -*- coding: utf-8 -*-
"""Windows 顶层窗口定位工具，不引入第三方 GUI 依赖。"""
from __future__ import annotations

import ctypes
import logging
import platform
import time
from ctypes import wintypes


logger = logging.getLogger(__name__)

_MONITORINFOF_PRIMARY = 1
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", wintypes.DWORD),
    ]


def calculate_center_position(
    work_area: tuple[int, int, int, int],
    window_size: tuple[int, int],
) -> tuple[int, int]:
    left, top, right, bottom = (int(value) for value in work_area)
    width, height = (max(1, int(value)) for value in window_size)
    return (
        left + max(0, (right - left - width) // 2),
        top + max(0, (bottom - top - height) // 2),
    )


def _primary_work_area(user32) -> tuple[int, int, int, int]:
    areas: list[tuple[int, int, int, int]] = []
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    monitor_callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_Rect),
        ctypes.c_void_p,
    )

    @monitor_callback_type
    def callback(monitor, _hdc, _monitor_rect, _data):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            if info.dwFlags & _MONITORINFOF_PRIMARY:
                rect = info.rcWork
                areas.append((rect.left, rect.top, rect.right, rect.bottom))
                return 0
        return 1

    if not user32.EnumDisplayMonitors(0, 0, callback, 0) or not areas:
        raise OSError("无法读取 Windows 主显示器工作区")
    return areas[0]


def _find_visible_window_for_pid(user32, process_id: int):
    found: list[int] = []
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.EnumWindows.restype = wintypes.BOOL
    enum_callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    @enum_callback_type
    def callback(hwnd, _data):
        if not user32.IsWindowVisible(hwnd):
            return 1
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == process_id:
            found.append(hwnd)
            return 0
        return 1

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def _window_size(user32, hwnd) -> tuple[int, int] | None:
    rect = _Rect()
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
    user32.GetWindowRect.restype = wintypes.BOOL
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)


def move_process_window_to_primary(
    process_id: int | None,
    *,
    timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> bool:
    """在 Selenium 连接前把指定进程的可见窗口移动到主显示器。"""
    if platform.system().lower() != "windows":
        return False
    try:
        pid = int(process_id or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        work_area = _primary_work_area(user32)
        deadline = time.monotonic() + max(0.0, float(timeout))
        interval = max(0.01, float(poll_interval))
        while True:
            hwnd = _find_visible_window_for_pid(user32, pid)
            if hwnd:
                size = _window_size(user32, hwnd)
                if size:
                    x, y = calculate_center_position(work_area, size)
                    flags = _SWP_NOZORDER | _SWP_NOACTIVATE
                    moved = user32.SetWindowPos(hwnd, 0, x, y, size[0], size[1], flags)
                    if moved:
                        logger.info(
                            "[Roxy窗口] 预定位主屏：pid=%s hwnd=%s x=%s y=%s width=%s height=%s",
                            pid,
                            int(hwnd),
                            x,
                            y,
                            size[0],
                            size[1],
                        )
                        return True
                    logger.warning("[Roxy窗口] SetWindowPos 失败：pid=%s hwnd=%s", pid, int(hwnd))
                    return False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    except Exception as exc:
        logger.warning("[Roxy窗口] 主屏预定位失败，继续执行：%s", exc)
        return False

    logger.warning("[Roxy窗口] 在 %.2f 秒内未找到窗口：pid=%s", max(0.0, float(timeout)), pid)
    return False

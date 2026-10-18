# -*- coding: utf-8 -*-
"""Window discovery and activation helpers (no third-party deps)."""
from __future__ import annotations
import ctypes, os, re, time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wt.HWND]
user32.SetFocus.argtypes = [wt.HWND]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                 wt.LPWSTR, ctypes.POINTER(wt.DWORD)]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9


def _proc_name(pid: int) -> str:
    """Exe basename. QueryFullProcessImageNameW works with LIMITED_INFORMATION,
    which GetModuleBaseNameW does not -- that mismatch silently blanked every row."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ''
    finally:
        kernel32.CloseHandle(h)


def list_windows():
    """Visible top-level windows with a title: [(hwnd, title, proc, pid)]."""
    out = []

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((hwnd, title, _proc_name(pid.value), pid.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def list_windows_ex():
    """Same as list_windows() but also returns the Win32 class name."""
    out = []
    real_cb = None

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            out.append((hwnd, title, _proc_name(pid.value), pid.value, cbuf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_by_cls(cls: str):
    for hwnd, title, proc, pid, c in list_windows_ex():
        if c == cls:
            return hwnd, title
    return 0, ''


user32.IsWindow.argtypes = [wt.HWND]


def is_window(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def pid_of_window(hwnd) -> int:
    try:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def win_key_of(hwnd) -> str:
    """'process|class' of a specific window handle."""
    if not is_window(hwnd):
        return ''
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cbuf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cbuf, 256)
    return '%s|%s' % (_proc_name(pid.value), cbuf.value)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    common = sum(1 for ch in set(a) if ch in b)
    return common / max(len(set(a)), 1) * 0.5


def _norm_title(s: str) -> str:
    s = (s or '').lower()
    s = re.sub(r'[0-9]+', '#', s)
    return re.sub(r'\s+', '', s)


def find_window_prefer(proc: str, cls: str, want_title: str = '', prefer_hwnd: int = 0):
    """Resolve a window by identity, preferring the exact handle that was
    recorded, then a title match. Several windows of one app are otherwise
    indistinguishable -- which is how an agent types into the wrong window."""
    key = '%s|%s' % (proc, cls)
    if prefer_hwnd and win_key_of(prefer_hwnd) == key:
        return prefer_hwnd, window_title(prefer_hwnd)
    cands = [(h, t) for h, t, p, pid, c in list_windows_ex()
             if c == cls and (not proc or p == proc)]
    if not cands:
        return 0, ''
    if want_title:
        nt = _norm_title(want_title)
        scored = sorted(cands, key=lambda ht: _similar(nt, _norm_title(ht[1])), reverse=True)
        if _similar(nt, _norm_title(scored[0][1])) > 0.35:
            return scored[0]
    return cands[0]


def find_window(proc: str, cls: str):
    """Window identity is (process, class): every Chromium app shares
    Chrome_WidgetWin_1, so matching on class alone picks the wrong window."""
    for hwnd, title, p, pid, c in list_windows_ex():
        if c == cls and (not proc or p == proc):
            return hwnd, title
    return 0, ''


def activate(hwnd: int, wait: float = 0.35):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    if fg != hwnd:
        tid = kernel32.GetCurrentThreadId()
        ftid = user32.GetWindowThreadProcessId(fg, None)
        attached = False
        try:
            # AttachThreadInput merges our input queue with another thread's.
            # If it is not undone, the shell can stop receiving input entirely,
            # so the detach must happen even when something in between throws.
            attached = bool(user32.AttachThreadInput(tid, ftid, True))
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid, ftid, False)
    if wait:
        time.sleep(wait)


def _tap_alt():
    """Synthetic Alt press unlocks SetForegroundWindow for a background process."""
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def ensure_foreground(hwnd: int, attempts: int = 2) -> bool:
    """Bring the window forward and CONFIRM it. Callers must not send bare
    keystrokes when this returns False."""
    if user32.GetForegroundWindow() == hwnd:
        return True
    for _ in range(attempts):
        activate(hwnd, wait=0.22)
        if user32.GetForegroundWindow() == hwnd:
            return True
        try:
            _tap_alt()
        except Exception:
            pass
        activate(hwnd, wait=0.25)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def hwnd_by_title(fragment: str) -> tuple[int, str]:
    for hwnd, title, _proc, _pid in list_windows():
        if fragment in title:
            return hwnd, title
    return 0, ''

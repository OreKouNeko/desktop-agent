# -*- coding: utf-8 -*-
"""Safety layer: panic hotkey, physical-input takeover, hard memory cap.

Three independent brakes, because a GUI agent that cannot be stopped is worse
than no agent at all:

  1. Ctrl+Alt+F12  -> RegisterHotKey, works even when the agent owns the input.
  2. Any real mouse/keyboard input by the human -> immediate pause. Events the
     agent injected carry LLKHF_INJECTED / LLMHF_INJECTED and are ignored, so
     the agent does not trip over its own actions.
  3. A Windows Job Object caps the whole process tree at 3 GB. If the agent
     leaks, Windows refuses the allocation and we die instead of the desktop.
"""
from __future__ import annotations
import ctypes, os, threading
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
VK_F12 = 0x7B

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD), ('flags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wt.POINT), ('mouseData', wt.DWORD), ('flags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wt.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wt.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wt.DWORD),
                ('SchedulingClass', wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_uint64),
                ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64),
                ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64),
                ('OtherTransferCount', ctypes.c_uint64)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


# wintypes.LPARAM is a 32-bit c_long even on 64-bit builds -- using it in a hook
# proc silently corrupts every callback. Declare the real pointer-sized types.
LRESULT = ctypes.c_ssize_t
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM_T, LPARAM_T)
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM_T, LPARAM_T]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]

# only deliberate mouse actions count as "a human grabbed the wheel";
# pointer drift across the desk is noise and must not abort a run.
MOUSE_BUTTON_MSGS = {0x0201, 0x0204, 0x0207, 0x020A, 0x020B}


class Guard:
    """Usage:  g = Guard(); ... if g.stop: abort ... if g.human: pause"""

    def __init__(self, memory_limit_gb: float = 3.0, hotkey=(MOD_CONTROL | MOD_ALT, VK_F12),
                 target_hwnd: int = 0, hooks: bool = False):
        self.stop = threading.Event()      # hard abort requested
        self.human = threading.Event()     # a real person touched the TARGET window
        self.target = target_hwnd
        # Global low-level hooks run INSIDE this process and need the GIL.
        # While the main thread holds the GIL (image encoding, hashing), every
        # keystroke and click in the whole OS waits on us; past
        # LowLevelHooksTimeout (300 ms default) Windows drops the event and the
        # desktop stops responding. That is a system-wide freeze caused by a
        # monitoring feature, so hooks are OFF unless explicitly requested.
        self.use_hooks = bool(hooks)
        self._hotkey = hotkey
        self._kb_hook = None
        self._ms_hook = None
        self._procs = []
        self._thread = None
        self._stop_msg_loop = False
        self._job = None
        if memory_limit_gb:
            self.apply_memory_cap(memory_limit_gb)

    # ---- 3. memory cap -------------------------------------------------
    def apply_memory_cap(self, gb: float):
        try:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(gb * 1024 ** 3)
            ok = kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                return False
            kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
            self._job = job
            return True
        except Exception:
            return False

    # ---- 1 + 2. hotkey and low-level input hooks ------------------------
    def set_target(self, hwnd: int):
        self.target = hwnd

    def _in_target(self, pt) -> bool:
        """True when the point falls inside the window the agent is driving."""
        if not self.target:
            return False
        r = wt.RECT()
        if not user32.GetWindowRect(self.target, ctypes.byref(r)):
            return False
        return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom

    def _hook_thread(self):
        mods, vk = self._hotkey
        try:
            user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk)
        except Exception:
            pass

        def _is_ours(hwnd) -> bool:
            if not hwnd:
                return False
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value == os.getpid()

        def kb_proc(nCode, wParam, lParam):
            if nCode == 0:
                try:
                    info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    # Only keyboard input aimed at the target window counts;
                    # typing anywhere else must not kill the run.
                    if (not (info.flags & LLKHF_INJECTED)
                            and self.target
                            and user32.GetForegroundWindow() == self.target):
                        self.human.set()
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def ms_proc(nCode, wParam, lParam):
            if nCode == 0 and wParam in MOUSE_BUTTON_MSGS:
                try:
                    info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    # A click is a takeover only if it lands on the target window.
                    if (not (info.flags & LLMHF_INJECTED)
                            and not _is_ours(self._hit(info.pt))
                            and self._in_target(info.pt)):
                        self.human.set()
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        if self.use_hooks:
            self._kb_cb = HOOKPROC(kb_proc)
            self._ms_cb = HOOKPROC(ms_proc)
            self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, None, 0)
            self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, None, 0)

        msg = wt.MSG()
        while not self._stop_msg_loop:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            if msg.message == WM_HOTKEY:
                self.stop.set()
        try:
            if self._kb_hook:
                user32.UnhookWindowsHookEx(self._kb_hook)
            if self._ms_hook:
                user32.UnhookWindowsHookEx(self._ms_hook)
            user32.UnregisterHotKey(None, 1)
        except Exception:
            pass

    def arm(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._hook_thread, daemon=True)
            self._thread.start()
        return self

    def disarm(self):
        self._stop_msg_loop = True
        user32.PostThreadMessageW(self._thread.ident, 0x0012, 0, 0)  # WM_QUIT
        self._thread = None

    @staticmethod
    def _hit(pt):
        try:
            user32.WindowFromPoint.restype = wt.HWND
            user32.WindowFromPoint.argtypes = [wt.POINT]
            return user32.WindowFromPoint(pt)
        except Exception:
            return 0

    # ---- helpers -------------------------------------------------------
    @property
    def aborted(self) -> bool:
        return self.stop.is_set() or self.human.is_set()

    def reason(self) -> str:
        if self.stop.is_set():
            return 'panic-hotkey'
        if self.human.is_set():
            return 'human-takeover'
        return ''

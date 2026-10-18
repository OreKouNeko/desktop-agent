# -*- coding: utf-8 -*-
"""Action layer: UIA patterns first (exact, no cursor movement), SendInput fallback."""
from __future__ import annotations
import ctypes, time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Clipboard handles are pointer-sized; without explicit prototypes ctypes
# truncates them to 32 bits and GlobalLock returns NULL on a 64-bit build.
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {
    'ctrl': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B,
    'enter': 0x0D, 'return': 0x0D, 'esc': 0x1B, 'escape': 0x1B,
    'tab': 0x09, 'space': 0x20, 'backspace': 0x08, 'delete': 0x2E,
    'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
    'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'insert': 0x2D, 'capslock': 0x14, 'numlock': 0x90,
}

for _i in range(10):
    VK[str(_i)] = 0x30 + _i
for _c in 'abcdefghijklmnopqrstuvwxyz':
    VK[_c] = ord(_c.upper())


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD),
                ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wt.DWORD), ('wParamL', wt.WORD), ('wParamH', wt.WORD)]


class _U(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', wt.DWORD), ('u', _U)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(INPUT))


def _key(vk, up=False):
    return INPUT(type=INPUT_KEYBOARD,
                 ki=KEYBDINPUT(wVk=vk, wScan=0,
                               dwFlags=KEYEVENTF_KEYUP if up else 0))


class Actions:
    """All methods return True on success."""

    def __init__(self, uia, dry_run=False):
        self.u = uia.u if uia is not None else None   # coordinate input needs no UIA
        self.dry_run = dry_run

    # ---- UIA pattern based (preferred: exact, no focus/cursor needed) ----
    def _pattern(self, node, pid, iface):
        try:
            p = node['el'].GetCurrentPattern(pid)
            return p.QueryInterface(iface)
        except Exception:
            return None

    def invoke(self, node):
        from comtypes.gen.UIAutomationClient import (
            UIA_InvokePatternId, IUIAutomationInvokePattern)
        p = self._pattern(node, UIA_InvokePatternId, IUIAutomationInvokePattern)
        if p is None:
            return False
        if self.dry_run:
            return True
        try:
            p.Invoke()
            return True
        except Exception:
            return False

    def set_value(self, node, text):
        from comtypes.gen.UIAutomationClient import (
            UIA_ValuePatternId, IUIAutomationValuePattern)
        p = self._pattern(node, UIA_ValuePatternId, IUIAutomationValuePattern)
        if p is None:
            return False
        if self.dry_run:
            return True
        try:
            p.SetValue(text)
            return True
        except Exception:
            return False

    def toggle(self, node):
        from comtypes.gen.UIAutomationClient import (
            UIA_TogglePatternId, IUIAutomationTogglePattern)
        p = self._pattern(node, UIA_TogglePatternId, IUIAutomationTogglePattern)
        if p is None:
            return False
        if self.dry_run:
            return True
        try:
            p.Toggle()
            return True
        except Exception:
            return False

    def select(self, node):
        from comtypes.gen.UIAutomationClient import (
            UIA_SelectionItemPatternId, IUIAutomationSelectionItemPattern)
        p = self._pattern(node, UIA_SelectionItemPatternId, IUIAutomationSelectionItemPattern)
        if p is None:
            return False
        if self.dry_run:
            return True
        try:
            p.Select()
            return True
        except Exception:
            return False

    def focus(self, node):
        if self.dry_run:
            return True
        try:
            node['el'].SetFocus()
            return True
        except Exception:
            return False

    # ---- coordinate / keyboard fallback ----
    def click(self, x, y, right=False):
        if self.dry_run:
            return True
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        ax = int(x * 65535 / max(sw - 1, 1))
        ay = int(y * 65535 / max(sh - 1, 1))
        down = MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP
        _send(
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(ax, ay, 0,
                  MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None)),
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(ax, ay, 0,
                  MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | down, 0, None)),
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(ax, ay, 0,
                  MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | up, 0, None)),
        )
        return True

    def _abs(self, x, y):
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return (int(x * 65535 / max(sw - 1, 1)), int(y * 65535 / max(sh - 1, 1)))

    def mouse_down(self, x, y, right=False):
        ax, ay = self._abs(x, y)
        flag = MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN
        _send(INPUT(type=INPUT_MOUSE,
                    mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | flag, 0, None)))

    def mouse_up(self, x, y, right=False):
        ax, ay = self._abs(x, y)
        flag = MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP
        _send(INPUT(type=INPUT_MOUSE,
                    mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | flag, 0, None)))

    def move_to(self, x, y):
        ax, ay = self._abs(x, y)
        _send(INPUT(type=INPUT_MOUSE,
                    mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None)))

    def drag(self, x1, y1, x2, y2, steps=16, delay=0.012):
        # Press, glide, release. The intermediate moves matter: jumping from A
        # straight to B is how a drag silently fails to register in most apps.
        if self.dry_run:
            return True
        self.move_to(x1, y1)
        time.sleep(0.03)
        self.mouse_down(x1, y1)
        time.sleep(0.05)
        for i in range(1, steps + 1):
            t = i / float(steps)
            self.move_to(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            time.sleep(delay)
        time.sleep(0.05)
        self.mouse_up(x2, y2)
        return True

    def double_click(self, x, y):
        if self.dry_run:
            return True
        self.click(x, y)
        time.sleep(0.06)
        self.click(x, y)
        return True

    def double_click_node(self, node):
        r = node.get('rect') if node else None
        if not r:
            return False
        return self.double_click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)

    def click_node(self, node, right=False):
        """Prefer Invoke pattern for left clicks.

        A right click must never use Invoke: that pattern has left-click
        semantics, so it activates the item instead of opening its context menu.
        """
        if not right and self.invoke(node):
            return True
        r = node.get('rect')
        if not r:
            return False
        return self.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2, right=right)

    def type_text(self, text):
        """Unicode injection: bypasses IME entirely, works with Chinese."""
        if self.dry_run:
            return True
        ins = []
        for ch in text:
            code = ord(ch)
            if code > 0xFFFF:
                continue  # surrogate pairs not needed for now
            ins.append(INPUT(type=INPUT_KEYBOARD,
                             ki=KEYBDINPUT(wVk=0, wScan=code,
                                           dwFlags=KEYEVENTF_UNICODE)))
            ins.append(INPUT(type=INPUT_KEYBOARD,
                             ki=KEYBDINPUT(wVk=0, wScan=code,
                                           dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)))
        if ins:
            _send(*ins)
        return True

    def set_clipboard(self, text):
        """Put text on the clipboard directly, without a helper process."""
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        data = text.encode('utf-16-le') + b'\x00\x00'
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return False
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            user32.CloseClipboard()

    def paste_text(self, text):
        """Clipboard paste: deterministic for CJK, no IME composition in the way.

        Unicode injection gets split by Excel's in-cell editor (it starts an IME
        composition), so Office cells need this path instead.
        """
        if self.dry_run:
            return True
        if not self.set_clipboard(text):
            return False
        time.sleep(0.08)
        self.hotkey('ctrl', 'v')
        return True

    def hotkey(self, *keys):
        if self.dry_run:
            return True
        vks = []
        for k in keys:
            k = k.lower()
            vk = VK.get(k)
            if vk is None and len(k) == 1:
                vk = ord(k.upper())
            if vk is None:
                raise ValueError('unknown key: %r' % k)   # never silently send 0
            vks.append(vk)
        ins = [_key(v) for v in vks] + [_key(v, up=True) for v in reversed(vks)]
        _send(*ins)
        return True

    def scroll(self, amount, x=None, y=None):
        if self.dry_run:
            return True
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        ax = int((x if x is not None else sw // 2) * 65535 / max(sw - 1, 1))
        ay = int((y if y is not None else sh // 2) * 65535 / max(sh - 1, 1))
        _send(INPUT(type=INPUT_MOUSE,
                    mi=MOUSEINPUT(ax, ay, amount & 0xFFFFFFFF,
                                  MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_WHEEL,
                                  0, None)))
        return True

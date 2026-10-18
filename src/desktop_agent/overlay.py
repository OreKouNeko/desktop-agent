# -*- coding: utf-8 -*-
"""Click-through overlay: draws boxes over the live desktop, no screen capture.

A layered, transparent, top-most, never-focusable window. Each update writes
premultiplied BGRA into a DIB section and hands it to UpdateLayeredWindow.
Cost is one memset + a few thousand pixel writes, so ~5-10 ms per step.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

H = ctypes.c_void_p
HDC = H
HBITMAP = H
HGDIOBJ = H
HWND = wt.HWND
HINSTANCE = wt.HINSTANCE
HMODULE = wt.HMODULE

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0
DIB_RGB_COLORS = 0
CW_USEDEFAULT = -2147483648

user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wt.WORD
user32.CreateWindowExW.restype = HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   HWND, wt.HMENU, HINSTANCE, ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [HWND, HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), HDC, ctypes.POINTER(wt.POINT),
                                       wt.DWORD, ctypes.c_void_p, wt.DWORD]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [HWND]

gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.CreateDIBSection.argtypes = [HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), H, wt.DWORD]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
kernel32.GetModuleHandleW.restype = HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('style', wt.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', HINSTANCE), ('hIcon', H), ('hCursor', H),
                ('hbrBackground', H), ('lpszMenuName', wt.LPCWSTR),
                ('lpszClassName', wt.LPCWSTR), ('hIconSm', H)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


_ATOM = [0]


class Overlay:
    def __init__(self, class_name: str = 'GuiAgentOverlay'):
        self.w = user32.GetSystemMetrics(0)
        self.h = user32.GetSystemMetrics(1)
        self._proc = WNDPROC(self._wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        if not _ATOM[0]:
            _ATOM[0] = user32.RegisterClassExW(ctypes.byref(wc))
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
              | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        self.hwnd = user32.CreateWindowExW(ex, class_name, 'agent-overlay',
                                           WS_POPUP, 0, 0, self.w, self.h,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError('CreateWindowExW failed: %d' % ctypes.get_last_error())
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = self.w
        bi.bmiHeader.biHeight = -self.h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        self._bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(None, ctypes.byref(bi), DIB_RGB_COLORS,
                                          ctypes.byref(self._bits), None, 0)
        self.mdc = gdi32.CreateCompatibleDC(None)
        gdi32.SelectObject(self.mdc, self.bmp)
        self.nbytes = self.w * self.h * 4
        self.buf = (ctypes.c_ubyte * self.nbytes).from_address(self._bits.value)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self.clear()

    @staticmethod
    def _wndproc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- pixel helpers (premultiplied BGRA) ---------------------------
    def _fill(self, l, t, r, b, rgb, alpha):
        l = max(0, min(self.w, int(l))); r = max(0, min(self.w, int(r)))
        t = max(0, min(self.h, int(t))); b = max(0, min(self.h, int(b)))
        if r <= l or b <= t:
            return
        cr, cg, cb = rgb
        a = alpha
        px = bytes((cb * a // 255, cg * a // 255, cr * a // 255, a))
        row = px * (r - l)
        stride = self.w * 4
        base = l * 4
        for y in range(t, b):
            off = y * stride + base
            self.buf[off:off + len(row)] = row

    def clear(self):
        ctypes.memset(self.buf, 0, self.nbytes)

    def boxes(self, items, thickness=3, tint=28):
        """items: [(l, t, r, b, (R,G,B))]"""
        self.clear()
        for item in items:
            l, t, r, b, rgb = item[:5]
            tn = item[5] if len(item) > 5 else tint
            if tn:
                self._fill(l, t, r, b, rgb, tn)
            self._fill(l, t, r, t + thickness, rgb, 255)
            self._fill(l, b - thickness, r, b, rgb, 255)
            self._fill(l, t, l + thickness, b, rgb, 255)
            self._fill(r - thickness, t, r, b, rgb, 255)
        self._present()

    def _present(self):
        pt_dst = wt.POINT(0, 0)
        size = wt.SIZE(self.w, self.h)
        pt_src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pt_dst),
                                   ctypes.byref(size), self.mdc, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), ULW_ALPHA)

    def hide(self):
        self.clear()
        self._present()

    def close(self):
        try:
            self.hide()
            user32.DestroyWindow(self.hwnd)
            gdi32.DeleteDC(self.mdc)
            gdi32.DeleteObject(self.bmp)
        except Exception:
            pass

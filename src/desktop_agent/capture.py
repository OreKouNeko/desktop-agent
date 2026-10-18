# -*- coding: utf-8 -*-
"""Screen capture: BitBlt / PrintWindow / StretchBlt + pure-stdlib PNG.

All GDI/user32 entry points are given explicit prototypes: without them ctypes
truncates 64-bit handles to 32 bits and eventually crashes.
"""
from __future__ import annotations
import ctypes, struct, zlib, time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

HDC = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p

user32.GetDC.restype = HDC
user32.GetDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = HDC
user32.GetWindowDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.PrintWindow.argtypes = [wt.HWND, HDC, wt.UINT]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleBitmap.restype = HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
gdi32.BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.StretchBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.DWORD]
gdi32.SetStretchBltMode.argtypes = [HDC, ctypes.c_int]
gdi32.GetDIBits.argtypes = [HDC, HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wt.UINT]
gdi32.SetDIBits.argtypes = [HDC, HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wt.UINT]

SRCCOPY = 0x00CC0020
HALFTONE = 4
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


def _bmi(w, h):
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = BI_RGB
    return bi


def _blit(hdc_src, x, y, w, h, dw, dh):
    mdc = gdi32.CreateCompatibleDC(hdc_src)
    bmp = gdi32.CreateCompatibleBitmap(hdc_src, dw, dh)
    old = gdi32.SelectObject(mdc, bmp)
    if (dw, dh) == (w, h):
        ok = gdi32.BitBlt(mdc, 0, 0, w, h, hdc_src, x, y, SRCCOPY)
    else:
        gdi32.SetStretchBltMode(mdc, HALFTONE)
        ok = gdi32.StretchBlt(mdc, 0, 0, dw, dh, hdc_src, x, y, w, h, SRCCOPY)
    bi = _bmi(dw, dh)
    buf = ctypes.create_string_buffer(dw * dh * 4)
    gdi32.GetDIBits(mdc, bmp, 0, dh, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    return bool(ok), buf.raw


def _png(bgra: bytes, w: int, h: int, level: int = 6) -> bytes:
    rows = bytearray()
    stride = w * 4
    for y in range(h):
        off = y * stride
        line = bgra[off:off + stride]
        rows.append(0)
        rgb = bytearray(w * 3)
        rgb[0::3] = line[2::4]
        rgb[1::3] = line[1::4]
        rgb[2::3] = line[0::4]
        rows += rgb

    def chunk(t, d):
        c = t + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(bytes(rows), level)) + chunk(b'IEND', b''))


def signature(hwnd: int, gw: int = 32, gh: int = 18) -> bytes:
    """A tiny grayscale thumbnail of a window, used to answer one question:
    did the pixels move? ~10 ms, independent of what UIA claims."""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return b''
    wdc = user32.GetWindowDC(hwnd)
    if not wdc:
        return b''
    try:
        # PrintWindow, not BitBlt: a composited window returns stale/black
        # pixels through a plain window DC.
        mdc = gdi32.CreateCompatibleDC(wdc)
        bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)   # PW_RENDERFULLCONTENT
        hdc2 = user32.GetDC(None)
        try:
            tdc = gdi32.CreateCompatibleDC(hdc2)
            tbmp = gdi32.CreateCompatibleBitmap(hdc2, gw, gh)
            told = gdi32.SelectObject(tdc, tbmp)
            gdi32.SetStretchBltMode(tdc, HALFTONE)
            gdi32.StretchBlt(tdc, 0, 0, gw, gh, mdc, 0, 0, w, h, SRCCOPY)
            bi = _bmi(gw, gh)
            buf = ctypes.create_string_buffer(gw * gh * 4)
            gdi32.GetDIBits(tdc, tbmp, 0, gh, buf, ctypes.byref(bi), DIB_RGB_COLORS)
            raw = buf.raw
            gdi32.SelectObject(tdc, told)
            gdi32.DeleteObject(tbmp)
            gdi32.DeleteDC(tdc)
        finally:
            user32.ReleaseDC(None, hdc2)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
    finally:
        user32.ReleaseDC(hwnd, wdc)
    out = bytearray(gw * gh)
    j = 0
    for i in range(0, len(raw), 4):
        out[j] = (raw[i] * 29 + raw[i + 1] * 150 + raw[i + 2] * 77) >> 8
        j += 1
    return bytes(out)


def sig_diff(a: bytes, b: bytes) -> float:
    """Mean absolute difference, 0.0 (identical) .. 1.0 (black vs white)."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def content_rect(hwnd: int):
    """The rectangle the captured image actually covers, in screen pixels.

    A DirectX fullscreen window reports a bogus position (often off-screen)
    while its client area really spans the display. Mapping clicks through that
    bogus rect sends every click to the wrong place, so screen-sized windows are
    clamped to the screen.
    """
    r = window_rect(hwnd)
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if not r:
        return (0, 0, sw, sh)
    w, h = r[2] - r[0], r[3] - r[1]
    inside = (r[0] >= 0 and r[1] >= 0 and r[2] <= sw and r[3] <= sh)
    if not inside and w >= sw * 0.95 and h >= sh * 0.95:
        return (0, 0, sw, sh)
    return r


def window_rect(hwnd: int):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return (r.left, r.top, r.right, r.bottom)


def sample(hwnd: int, gw: int = 160):
    """Small BGRA snapshot of a window, for local pixel analysis."""
    r = window_rect(hwnd)
    if not r:
        return b'', 0, 0
    w, h = r[2] - r[0], r[3] - r[1]
    gh = max(1, int(h * gw / float(w)))
    wdc = user32.GetWindowDC(hwnd)
    if not wdc:
        return b'', 0, 0
    try:
        mdc = gdi32.CreateCompatibleDC(wdc)
        bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)
        hdc2 = user32.GetDC(None)
        try:
            tdc = gdi32.CreateCompatibleDC(hdc2)
            tbmp = gdi32.CreateCompatibleBitmap(hdc2, gw, gh)
            told = gdi32.SelectObject(tdc, tbmp)
            gdi32.SetStretchBltMode(tdc, HALFTONE)
            gdi32.StretchBlt(tdc, 0, 0, gw, gh, mdc, 0, 0, w, h, SRCCOPY)
            bi = _bmi(gw, gh)
            buf = ctypes.create_string_buffer(gw * gh * 4)
            gdi32.GetDIBits(tdc, tbmp, 0, gh, buf, ctypes.byref(bi), DIB_RGB_COLORS)
            raw = buf.raw
            gdi32.SelectObject(tdc, told)
            gdi32.DeleteObject(tbmp)
            gdi32.DeleteDC(tdc)
        finally:
            user32.ReleaseDC(None, hdc2)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
    finally:
        user32.ReleaseDC(hwnd, wdc)
    return raw, gw, gh


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def grab_screen(target_width: int = 1280):
    sw, sh = screen_size()
    th = max(1, int(sh * target_width / sw))
    hdc = user32.GetDC(None)
    try:
        _, raw = _blit(hdc, 0, 0, sw, sh, target_width, th)
    finally:
        user32.ReleaseDC(None, hdc)
    return _png(raw, target_width, th), target_width, th


def grab_window(hwnd: int, target_width: int = 1280):
    """Capture one window. PrintWindow works even when partially occluded."""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None, 0, 0
    wdc = user32.GetWindowDC(hwnd)
    if not wdc:
        return None, 0, 0
    try:
        mdc = gdi32.CreateCompatibleDC(wdc)
        bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)      # PW_RENDERFULLCONTENT
        bi = _bmi(w, h)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
    finally:
        user32.ReleaseDC(hwnd, wdc)
    raw = buf.raw
    if target_width < w:
        th = max(1, int(h * target_width / w))
        hdc2 = user32.GetDC(None)
        try:
            tmp = gdi32.CreateCompatibleDC(hdc2)
            bmp2 = gdi32.CreateCompatibleBitmap(hdc2, w, h)
            o2 = gdi32.SelectObject(tmp, bmp2)
            bi2 = _bmi(w, h)
            src = ctypes.create_string_buffer(raw, len(raw))
            gdi32.SetDIBits(tmp, bmp2, 0, h, src, ctypes.byref(bi2), DIB_RGB_COLORS)
            _, raw = _blit(tmp, 0, 0, w, h, target_width, th)
            gdi32.SelectObject(tmp, o2)
            gdi32.DeleteObject(bmp2)
            gdi32.DeleteDC(tmp)
        finally:
            user32.ReleaseDC(None, hdc2)
        return _png(raw, target_width, th), target_width, th
    return _png(raw, w, h), w, h

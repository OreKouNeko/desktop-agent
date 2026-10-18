# -*- coding: utf-8 -*-
"""API key at rest: Windows DPAPI, bound to the current user account."""
from __future__ import annotations
import ctypes
from ctypes import wintypes as wt

crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(text: str) -> bytes:
    src, keep = _blob(text.encode('utf-8'))
    out = DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None,
                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError('CryptProtectData failed: %d' % kernel32.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def unprotect(blob: bytes) -> str:
    src, keep = _blob(blob)
    out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError('CryptUnprotectData failed: %d' % kernel32.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode('utf-8')
    finally:
        kernel32.LocalFree(out.pbData)

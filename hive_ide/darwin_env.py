"""Read macOS process environments without flattening argv and env into text."""

from __future__ import annotations

import ctypes
import struct


def process_environ(pid: int) -> bytes:
    """Return NUL-separated env entries, or no evidence on a failed observation.

    KERN_PROCARGS2 returns argc, the executable path, padding, argv, then env.
    Unlike `ps eww`, it preserves spaces and cannot mistake an argv assignment
    for the pane's identity. Constants/layout follow Apple's sysctl.h and ps.
    """
    try:
        sysctl = ctypes.CDLL(None).sysctl
        sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        argmax = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        if sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
            return b""
        if not 0 < argmax.value <= 16 * 1024 * 1024:
            return b""
        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(len(buf))
        if sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(size), None, 0):
            return b""
        return _environ_from_procargs(buf.raw[:size.value])
    except (OSError, AttributeError):
        return b""


def _environ_from_procargs(raw: bytes) -> bytes:
    header = struct.calcsize("i")
    if len(raw) < header:
        return b""
    argc = struct.unpack_from("i", raw)[0]
    if argc <= 0 or argc > len(raw):
        return b""
    offset = raw.find(b"\0", header)
    if offset < 0:
        return b""
    # tmux launches our wrapper with a nonempty argv[0]. PROCARGS2 cannot
    # distinguish padding from an empty argv[0]; do not use this to identify
    # arbitrary processes deliberately launched with that unsupported shape.
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    # Do not skip empty argv entries: they still count towards argc.
    for _ in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            return b""
        offset = end + 1
    entries = []
    while offset < len(raw):
        end = raw.find(b"\0", offset)
        if end < 0:
            return b""
        entry = raw[offset:end]
        if not entry:
            return b"\0".join(entries)
        if b"=" not in entry:
            return b""
        entries.append(entry)
        offset = end + 1
    return b"\0".join(entries)

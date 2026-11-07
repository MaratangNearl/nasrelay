"""Read birth time where available; Unix ctime is not a creation date."""
import ctypes
import os
import sys
from datetime import datetime, timezone


class _Timestamp(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_int64), ('nanoseconds', ctypes.c_uint32),
                ('reserved', ctypes.c_int32)]


class _Statx(ctypes.Structure):
    _fields_ = [('mask', ctypes.c_uint32), ('block_size', ctypes.c_uint32),
                ('attributes', ctypes.c_uint64), ('links', ctypes.c_uint32),
                ('uid', ctypes.c_uint32), ('gid', ctypes.c_uint32),
                ('mode', ctypes.c_uint16), ('reserved', ctypes.c_uint16),
                ('inode', ctypes.c_uint64), ('size', ctypes.c_uint64),
                ('blocks', ctypes.c_uint64), ('attributes_mask', ctypes.c_uint64),
                ('accessed', _Timestamp), ('born', _Timestamp),
                ('changed', _Timestamp), ('modified', _Timestamp),
                ('rdev_major', ctypes.c_uint32), ('rdev_minor', ctypes.c_uint32),
                ('dev_major', ctypes.c_uint32), ('dev_minor', ctypes.c_uint32),
                ('spare', ctypes.c_uint64 * 14)]


_statx = None
if sys.platform == 'linux':
    _statx = getattr(ctypes.CDLL(None), 'statx', None)
    if _statx is not None:
        _statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_uint, ctypes.POINTER(_Statx)]
        _statx.restype = ctypes.c_int


def creation_time(path, info):
    timestamp = getattr(info, 'st_birthtime', None)
    if timestamp is None and _statx is not None:
        result = _Statx()
        # AT_FDCWD, AT_SYMLINK_NOFOLLOW, STATX_BTIME. Unsupported filesystems
        # simply omit the birth-time bit, even if the call itself succeeds.
        if _statx(-100, os.fsencode(path), 0x100, 0x800, ctypes.byref(result)) == 0 and result.mask & 0x800:
            timestamp = result.born.seconds + result.born.nanoseconds / 1_000_000_000
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else ''

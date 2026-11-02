"""Read-only, local process incarnation checks; never signal or mutate a process.

snapshot(pid) returns JSON containing only platform, pid, uid, birth and an
actual executable-content SHA-256. validate(value) checks the transport schema
without assuming the receiver's platform/UID. verify(value) requires an exact
fresh local snapshot, including local ownership and a live, non-zombie process.

This does NOT authenticate a native session, source file or operator. A trusted
operator must separately enroll that mapping. Birth checks bound PID reuse;
they do not make a later action atomic or detect exec of identical bytes. The
caller must register kernel exit/exec observation with checks around enrollment
and handle its events before trusting this identity for subsequent source reads.
No executable path, command, argv, file contents or underlying OS errors escape.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import sys

from .native_result_ledger import Refused


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_MAX_EXECUTABLE = 512 * 1024 * 1024


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise Refused("invalid process identity fields")


def validate(value):
    """Return a fresh strict JSON value, without local runtime assumptions."""
    _fields(value, ("platform", "pid", "uid", "birth", "executable_sha256"))
    platform = value["platform"]
    if type(platform) is not str or platform not in ("darwin", "linux"):
        raise Refused("unsupported process identity platform")
    if not _integer(value["pid"], 1, 2**31 - 1):
        raise Refused("invalid process PID")
    if not _integer(value["uid"], 0, 2**32 - 1):
        raise Refused("invalid process UID")
    digest = value["executable_sha256"]
    if type(digest) is not str or not _SHA256.fullmatch(digest):
        raise Refused("invalid process executable digest")
    birth = value["birth"]
    if platform == "darwin":
        _fields(birth, ("tv_sec", "tv_usec"))
        if (not _integer(birth["tv_sec"], 1, 2**64 - 1)
                or not _integer(birth["tv_usec"], 0, 999999)):
            raise Refused("invalid process birth")
    else:
        _fields(birth, ("start_ticks", "boot_id"))
        if (not _integer(birth["start_ticks"], 0, 2**64 - 1)
                or type(birth["boot_id"]) is not str
                or not _BOOT_ID.fullmatch(birth["boot_id"])):
            raise Refused("invalid process birth")
    return {"platform": platform, "pid": value["pid"], "uid": value["uid"],
            "birth": dict(birth), "executable_sha256": digest}


def _file_key(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid)


def _digest(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_EXECUTABLE:
        raise Refused("unreadable process executable")
    digest, size = hashlib.sha256(), 0
    while block := os.read(fd, 1024 * 1024):
        size += len(block)
        if size > _MAX_EXECUTABLE:
            raise Refused("process executable exceeds bound")
        digest.update(block)
    key = _file_key(before)
    if size != before.st_size or _file_key(os.fstat(fd)) != key:
        raise Refused("process executable changed during read")
    return digest.hexdigest(), key


# Apple ABI: xnu/bsd/sys/proc_info.h, struct proc_bsdinfo (MAXCOMLEN=16),
# PROC_PIDTBSDINFO=3; xnu/libsyscall/wrappers/libproc/libproc.h signatures.
# https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h
class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "pjobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                 ("start_usec", ctypes.c_uint64)]


def _darwin_library():
    if ctypes.sizeof(_ProcBsdInfo) != 136:
        raise Refused("unsupported process ABI")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    return library


def _darwin_info(library, pid):
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size:
        raise Refused("live process identity unavailable")
    # SRUN=2, SSLEEP=3, SSTOP=4; reject SIDL, SZOMB and PROC_FLAG_INEXIT.
    if info.pid != pid or info.status not in (2, 3, 4) or info.flags & 4:
        raise Refused("live non-zombie process required")
    if any(uid != os.getuid() for uid in (info.uid, info.ruid, info.svuid)):
        raise Refused("locally owned process required")
    return info.uid, {"tv_sec": info.start_sec, "tv_usec": info.start_usec}


def _darwin_path(library, pid):
    buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    length = library.proc_pidpath(pid, buffer, len(buffer))
    value = buffer.value
    if length <= 0 or length >= len(buffer) or not value.startswith(b"/"):
        raise Refused("process executable unavailable")
    return value


def _darwin_snapshot(pid):
    library = _darwin_library()
    before = _darwin_info(library, pid)
    path = _darwin_path(library, pid)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        digest, key = _digest(fd)
        if (_darwin_path(library, pid) != path or _file_key(os.stat(path)) != key
                or _darwin_info(library, pid) != before):
            raise Refused("process identity changed during snapshot")
    finally:
        os.close(fd)
    return before, digest


def _read_at(directory, name, bound):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        data = os.read(fd, bound + 1)
        if len(data) > bound:
            raise Refused("process metadata exceeds bound")
        return data
    finally:
        os.close(fd)


def _linux_info(directory, pid):
    raw = _read_at(directory, "stat", 65536)
    # comm may contain spaces and parentheses; fields after the LAST ') ' are
    # fixed, starting at state (field 3), so starttime (field 22) is index 19.
    prefix, separator, tail = raw.rpartition(b") ")
    fields = tail.split()
    if (not separator or prefix.partition(b" (")[0] != str(pid).encode()
            or len(fields) < 20 or fields[0] not in (b"R", b"S", b"D", b"T",
                                                     b"t", b"W", b"K", b"P", b"I")):
        raise Refused("live non-zombie process required")
    status = _read_at(directory, "status", 65536)
    rows = [line.split()[1:] for line in status.splitlines() if line.startswith(b"Uid:")]
    if len(rows) != 1 or len(rows[0]) != 4:
        raise Refused("process ownership unavailable")
    if any(int(uid) != os.getuid() for uid in rows[0]):
        raise Refused("locally owned process required")
    return os.getuid(), int(fields[19])


def _linux_snapshot(pid):
    directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = _linux_info(directory, pid)
        fd = os.open("exe", os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory)
        try:
            digest, key = _digest(fd)
            if (_file_key(os.stat("exe", dir_fd=directory)) != key
                    or _linux_info(directory, pid) != before):
                raise Refused("process identity changed during snapshot")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    with open("/proc/sys/kernel/random/boot_id", "rb") as stream:
        boot_id = stream.read(64).strip().decode("ascii")
    return (before[0], {"start_ticks": before[1], "boot_id": boot_id}), digest


def snapshot(pid):
    """Snapshot a currently owned live process; all failures are scrubbed."""
    if not _integer(pid, 1, 2**31 - 1):
        raise Refused("invalid process PID")
    if sys.platform not in ("darwin", "linux"):
        raise Refused("unsupported process identity platform")
    try:
        (uid, birth), digest = (_darwin_snapshot(pid) if sys.platform == "darwin"
                                else _linux_snapshot(pid))
        return validate({"platform": sys.platform, "pid": pid, "uid": uid,
                         "birth": birth, "executable_sha256": digest})
    except Refused:
        raise
    except (OSError, ValueError, UnicodeError, AttributeError, OverflowError):
        raise Refused("process identity unavailable") from None


def verify(value):
    """Return the identity only if an exact fresh, locally owned match exists."""
    expected = validate(value)
    if expected["platform"] != sys.platform or expected["uid"] != os.getuid():
        raise Refused("process identity is not local and owned")
    if snapshot(expected["pid"]) != expected:
        raise Refused("process identity mismatch")
    return expected

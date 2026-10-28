"""Harness adapter contract.

An adapter turns "run this prompt for this task, in this directory" into a
detached process whose structured event stream is written to a file. The
runner never parses a terminal screen: everything it needs (native session id,
final message, cost) comes from that stream.

Processes run in their own process group. A turn is over only when the whole
group is gone, so a background command the agent left running keeps the slot
occupied instead of racing the next writer.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TurnResult:
    session_id: str = ""
    final_text: str = ""
    is_error: bool = False
    error: str = ""
    cost_usd: float | None = None
    usage: dict = field(default_factory=dict)
    finished: bool = False          # the stream contains a terminal event
    model: str = ""
    compactions: int = 0            # context compactions the harness reported in this turn
    error_details: str = field(default="", repr=False)  # full local-only cause, never a display summary
    auth_failed: bool = False       # an adapter-verified terminal authentication banner
    auth_only: bool = False         # no assistant/tool work preceded that banner


@dataclass
class Launch:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    prompt: str


class Adapter:
    name = "base"

    def __init__(self, binary: str, extra_args: list[str] | None = None):
        self.binary = binary
        self.extra_args = list(extra_args or [])

    # -- to implement ----------------------------------------------------------------
    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        raise NotImplementedError

    def prepare_resume(self, session_id: str, cwd: Path, env: dict) -> str:
        """Make the session resumable under ``env`` before a resume. Returns a note
        when it had to do something, '' otherwise."""
        return ""

    def session_known(self, session_id: str, cwd: Path, env: dict) -> bool:
        """False only when the harness certainly cannot resume ``session_id`` under ``env``
        (checked after ``prepare_resume``); a harness that cannot tell says True."""
        return True

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        raise NotImplementedError

    def parse(self, log_path: Path) -> TurnResult:
        raise NotImplementedError

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        """argv with the reasoning effort set for this session; a harness without
        one returns argv unchanged."""
        return argv

    def with_model(self, argv: list[str], model: str) -> list[str]:
        """argv running ``model`` instead of the configured one. Harnesses whose
        model comes from elsewhere (a Codex profile) ignore it."""
        return argv

    def with_read_only(self, argv: list[str]) -> list[str]:
        """argv for an attempt that must not change files or run commands (a
        metadata-only decision). The harness enforces it, not the prompt."""
        raise NotImplementedError

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        """argv for a session that may write, but only inside its cwd: enforced by the harness."""
        raise NotImplementedError

    def without_local_memory(self, env: dict) -> dict:
        """env for a session that must not keep the harness's own memory files (a chat: read-only and
        shared by several people, so its memory is Mesh). A harness without such memory returns env."""
        return env

    def with_instructions(self, argv: list[str], text: str) -> list[str]:
        """argv with ``text`` added to the session's standing instructions. A harness without
        such a channel returns argv unchanged."""
        return argv

    def with_mcp(self, argv: list[str], name: str, command: list[str], env: dict) -> list[str]:
        """argv whose MCP server ``name`` is the stdio ``command`` with ``env``, in place of
        whatever the profile configures under that name. A harness without MCP returns argv."""
        return argv

    @property
    def session_id_known_upfront(self) -> bool:
        """True when the runner chooses the session id before starting (Claude)."""
        return False

    # -- shared process handling -----------------------------------------------------------
    def spawn(self, launch: Launch, log_path: Path) -> subprocess.Popen:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab")
        err = open(log_path.with_suffix(".stderr"), "ab")
        try:
            proc = subprocess.Popen(launch.argv, cwd=str(launch.cwd), env=launch.env,
                                    stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    start_new_session=True)
        finally:
            out.close()
            err.close()
        assert proc.stdin is not None
        try:
            proc.stdin.write(launch.prompt.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass                       # the process died at once; settle reads its stream
        return proc


def read_events(log_path: Path) -> list[dict]:
    events = []
    try:
        with open(log_path, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw.startswith(b"{"):
                    continue
                try:
                    events.append(json.loads(raw))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return events


STAMP_V2 = "v2:"


def legacy_stamp(pid: int) -> str:
    """Start time as `ps` prints it (one-second precision); the pre-v2 stamp format."""
    if pid <= 0:
        return ""
    res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return res.stdout.strip()


def _format_v2(seconds: int, micros: int) -> str:
    return f"{STAMP_V2}{seconds}.{micros:06d}"


def _darwin_start(pid: int) -> str:
    """kinfo_proc.kp_proc.p_starttime via sysctl kern.proc.pid.

    Layout (<sys/sysctl.h>, <sys/proc.h>): `struct kinfo_proc` starts with `struct extern_proc
    kp_proc`, whose first member is `union p_un { struct { proc *__p_forw, *__p_back } p_st1;
    struct timeval __p_starttime; }` (p_starttime is `#define`d to p_un.__p_starttime). So the
    start time is the timeval at offset 0: int64 tv_sec, then int32 tv_usec (suseconds_t) on
    64-bit Darwin. tests/test_pid_reuse.py checks it against the moment a child was spawned."""
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)          # CTL_KERN, KERN_PROC, KERN_PROC_PID, pid
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value < 16:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
        return ""
    seconds = int.from_bytes(buf.raw[0:8], "little", signed=True)
    micros = int.from_bytes(buf.raw[8:12], "little", signed=True)
    if seconds <= 0 or not 0 <= micros < 1_000_000:
        return ""
    return _format_v2(seconds, micros)


def _linux_start(pid: int, proc_root: str = "/proc") -> str:
    """starttime (clock ticks since boot, field 22 of stat) plus the boot time."""
    with open(f"{proc_root}/{pid}/stat") as fh:
        rest = fh.read().rsplit(")", 1)[1].split()
    ticks = int(rest[19])                            # fields after the command start at field 3
    boot = 0
    with open(f"{proc_root}/stat") as fh:
        for line in fh:
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
    if boot <= 0:
        return ""
    hz = os.sysconf("SC_CLK_TCK")
    total_micros = boot * 1_000_000 + ticks * 1_000_000 // hz
    return _format_v2(total_micros // 1_000_000, total_micros % 1_000_000)


def precise_stamp(pid: int) -> str:
    """Sub-second start time as `v2:<seconds>.<microseconds>`, or "" when it cannot be read."""
    if pid <= 0:
        return ""
    try:
        if sys.platform == "darwin":
            return _darwin_start(pid)
        if sys.platform.startswith("linux"):
            return _linux_start(pid)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return ""


def proc_stamp(pid: int) -> str:
    """Start time of a process, to tell our process from a reused pid. Sub-second when the
    platform allows it (a pid reused within the same second still differs); otherwise the
    legacy `ps` string."""
    if pid <= 0:
        return ""
    return precise_stamp(pid) or legacy_stamp(pid)


def stamp_differs(pid: int, stamp: str) -> bool:
    """True when a stamp is stored and a live process now holds this pid with another start
    time. A legacy stamp (no `v2:` prefix) is compared at the old precision against the
    current legacy string, so attempts stored before the format changed stay recognised. A
    v2 stamp whose precise read fails now is treated as the same process (conservative)."""
    if pid <= 0 or not stamp:
        return False
    if stamp.startswith(STAMP_V2):
        current = precise_stamp(pid)
    else:
        current = legacy_stamp(pid)
    return bool(current) and current != stamp


def pid_reused(pid: int, stamp: str) -> bool:
    """True when the stored stamp proves the pid now belongs to another process. POSIX never
    reuses a pid while a process group with that id exists, so the group that stamp belonged
    to is gone."""
    return stamp_differs(pid, stamp)


def group_alive(pgid: int, stamp: str = "") -> bool:
    """True while any process of the attempt's process group exists. With a
    stamp, a live leader whose start time differs is a reused pid: not ours."""
    if pgid <= 0:
        return False
    if pid_reused(pgid, stamp):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def interrupt_group(pgid: int, sig: int = signal.SIGINT) -> None:
    if pgid > 0:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

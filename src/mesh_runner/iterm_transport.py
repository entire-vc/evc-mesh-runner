"""Local iTerm transport of the strict existing-session metadata receiver (default off; ``transport = "iterm"``).

Supported interface: iTerm2's own AppleScript dictionary through ``/usr/bin/osascript`` — the ONLY local interface used. It
addresses a terminal session by its ``unique id`` (stable for the life of that session, never reused for another one), reads
its ``tty`` and writes text into that session directly. It needs the Automation access that the process running the
receiver ALREADY has for iTerm; this module never asks for, installs or works around a grant. No grant (-1743), iTerm not
running, an unknown or duplicated session id: refusal, zero injection. Nothing here uses SSH/CDP, keystrokes, the frontmost
window, a new window/tab/session, or the producer's lifecycle.

Binding (terminal session <-> native producer <-> host <-> runner), proved at every delivery:
  1. the pinned iTerm ``unique id`` exists exactly once and its ``tty`` equals the pinned tty (read inside the SAME script
     that writes, so a session replaced between check and write cannot be hit);
  2. the pinned producer pid is alive with exactly the pinned start time (``ps lstart``), sits on that same tty and is the
     foreground process group of it (``pgid == tpgid``), i.e. it is what actually reads that terminal;
  3. host_id/runner_id/session_id come from the admitted identity, and the whole pin (terminal id, tty, pid, start, those
     ids) is hashed into the ledger and admission records (``session_receiver.transport_digest``): another terminal or
     another producer needs a new ledger/admission, never a silent re-bind.
A writer PID or a window title is never evidence.

Known limits (reported, not worked around):
  * no cross-process atomicity exists: the write script re-proves tty, producer start and foreground group itself, before the
    paste and again immediately before the Enter. The paste is bracketed, so if the producer exits in between, the shell
    that inherits the tty gets inert, unsubmitted text (nothing executes without the Enter, which is sent only after the
    second proof); the residual window is the few milliseconds between that proof and the Enter. An unconfirmed or
    interrupted write is journalled ``uncertain`` and never replayed.
  * text is delivered as a bracketed paste (ESC[200~ … ESC[201~, then Enter), like ``tmux paste-buffer -p``. A producer that
    has not enabled bracketed paste would show the markers; Claude Code and Codex TUIs enable it. This cannot be probed.
  * iTerm has no "is the prompt idle" signal here; an injection while the producer is busy is queued by its TUI, same as tmux.
"""
from __future__ import annotations

import logging
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mesh_runner.iterm_transport")

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 15                                            # every osascript/ps call is finite
UNIQUE_ID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
TTY = re.compile(r"^/dev/ttys\d{3,}$")

# Fixed scripts: every value arrives as argv, never interpolated into the script text.
_FIND = """
on findSession(sid)
  tell application "iTerm"
    set hits to {}
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (unique id of s) is sid then set end of hits to s
        end repeat
      end repeat
    end repeat
    if (count of hits) is not 1 then error "iterm_target_" & (count of hits)
    return item 1 of hits
  end tell
end findSession
"""
PROBE_SCRIPT = _FIND + """
on run argv
  set s to findSession(item 1 of argv)
  tell application "iTerm" to return tty of s
end run
"""
# The producer is re-proved INSIDE this call, before the paste and again right before the Enter that would submit it. The
# paste is bracketed, so a shell that ended up owning the tty receives inert text, never a command: only the Enter can
# execute, and it is sent only after the second proof. argv: id, tty, text, producer pid, producer start (`ps lstart`).
WRITE_SCRIPT = _FIND + """
on producerOk(pid, started, tty)
  set row to do shell script "/bin/ps -o pgid=,tpgid=,tty=,lstart= -p " & (quoted form of pid) & " || true"
  set AppleScript's text item delimiters to " "
  set parts to text items of row
  set AppleScript's text item delimiters to ""
  set clean to {}
  repeat with x in parts
    if (x as text) is not "" then set end of clean to (x as text)
  end repeat
  if (count of clean) < 4 then return false
  if (item 1 of clean) is not (item 2 of clean) then return false
  if ("/dev/" & (item 3 of clean)) is not tty then return false
  set AppleScript's text item delimiters to " "
  set lstart to (items 4 thru -1 of clean) as text
  set AppleScript's text item delimiters to ""
  return lstart is started
end producerOk

on run argv
  set s to findSession(item 1 of argv)
  set tty0 to item 2 of argv
  tell application "iTerm"
    if (tty of s) is not tty0 then error "iterm_tty_changed"
    if not my producerOk(item 4 of argv, item 5 of argv, tty0) then error "iterm_producer_changed"
    tell s
      write text ((ASCII character 27) & "[200~" & (item 3 of argv) & (ASCII character 27) & "[201~") newline NO
    end tell
    if (tty of s) is not tty0 then error "iterm_tty_changed_after_paste"
    if not my producerOk(item 4 of argv, item 5 of argv, tty0) then error "iterm_producer_changed_after_paste"
    tell s to write text ""
  end tell
  return "ok"
end run
"""


_PRODUCER_OK = """
on producerOk(pid, started, tty)
  set row to do shell script "/bin/ps -o pgid=,tpgid=,tty=,lstart= -p " & (quoted form of pid) & " || true"
  set AppleScript's text item delimiters to " "
  set parts to text items of row
  set AppleScript's text item delimiters to ""
  set clean to {}
  repeat with x in parts
    if (x as text) is not "" then set end of clean to (x as text)
  end repeat
  if (count of clean) < 4 then return false
  if (item 1 of clean) is not (item 2 of clean) then return false
  if ("/dev/" & (item 3 of clean)) is not tty then return false
  set AppleScript's text item delimiters to " "
  set lstart to (items 4 thru -1 of clean) as text
  set AppleScript's text item delimiters to ""
  return lstart is started
end producerOk

"""

# Two-step variant for the host-local QA assignment: STAGE proves the binding and pastes WITHOUT Enter; the caller then
# asks its authority again (backend check + local binding) and only then runs SUBMIT, which re-proves tty and producer and
# sends the Enter. argv of both: id, tty, producer pid, producer start; STAGE additionally has the text as argv 5.
STAGE_SCRIPT = _FIND + _PRODUCER_OK + """
on run argv
  set s to findSession(item 1 of argv)
  set tty0 to item 2 of argv
  tell application "iTerm"
    if (tty of s) is not tty0 then error "iterm_tty_changed"
    if not my producerOk(item 3 of argv, item 4 of argv, tty0) then error "iterm_producer_changed"
    tell s
      write text ((ASCII character 27) & "[200~" & (item 5 of argv) & (ASCII character 27) & "[201~") newline NO
    end tell
    if (tty of s) is not tty0 then error "iterm_tty_changed_after_paste"
    if not my producerOk(item 3 of argv, item 4 of argv, tty0) then error "iterm_producer_changed_after_paste"
  end tell
  return "ok"
end run
"""
SUBMIT_SCRIPT = _FIND + _PRODUCER_OK + """
on run argv
  set s to findSession(item 1 of argv)
  set tty0 to item 2 of argv
  tell application "iTerm"
    if (tty of s) is not tty0 then error "iterm_tty_changed_after_paste"
    if not my producerOk(item 3 of argv, item 4 of argv, tty0) then error "iterm_producer_changed_after_paste"
    tell s to write text ""
  end tell
  return "ok"
end run
"""


@dataclass(frozen=True)
class ITermPin:
    iterm_session_id: str        # iTerm `unique id` of the exact terminal session
    tty: str                     # its tty, "/dev/ttysNNN"
    producer_pid: int            # the native producer that reads that terminal
    producer_started: str        # `ps -o lstart=` baseline of that pid


def valid_pin(pin: ITermPin) -> str:
    """"" when the pin is well formed, else the typed reason (a malformed pin must never reach a script)."""
    if not UNIQUE_ID.match(pin.iterm_session_id):
        return "iterm_session_id_invalid"
    if not TTY.match(pin.tty):
        return "iterm_tty_invalid"
    return ""


def _run(argv: list[str], stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=stdin)


def _reason(res: subprocess.CompletedProcess) -> str:
    err = res.stderr or ""
    if "-1743" in err:
        return "automation_not_granted"
    if "-600" in err or "isn't running" in err or "not running" in err:
        return "iterm_not_running"
    m = re.search(r"iterm_target_(\d+)", err)
    if m:
        return "target_missing" if m.group(1) == "0" else "target_ambiguous"
    if "after_paste" in err:
        return "changed_after_paste"                          # text was pasted but NOT submitted
    if "iterm_producer_changed" in err:
        return "producer_changed"
    if "iterm_tty_changed" in err:
        return "tty_changed"
    return "iterm_unavailable"


class ITermAPI:
    """The two osascript calls, behind one seam so tests (and the preflight) never need a live iTerm."""

    def __init__(self, osascript: str = OSASCRIPT):
        self.osascript = osascript

    def probe(self, session_id: str) -> tuple[str, str]:
        """``(tty, "")`` or ``("", reason)``. Read only."""
        try:
            res = _run([self.osascript, "-e", PROBE_SCRIPT, session_id])
        except (OSError, subprocess.TimeoutExpired):
            return "", "iterm_unavailable"
        if res.returncode != 0:
            return "", _reason(res)
        return res.stdout.strip(), ""

    def write(self, session_id: str, tty: str, text: str, producer_pid: int, producer_started: str) -> bool:
        try:
            res = _run([self.osascript, "-e", WRITE_SCRIPT, session_id, tty, text, str(producer_pid),
                        " ".join(producer_started.split())])
        except (OSError, subprocess.TimeoutExpired):
            return False
        if res.returncode != 0:
            log.warning("iterm write refused: %s", _reason(res))        # never the text, never raw stderr
            return False
        return res.stdout.strip() == "ok"

    def stage(self, session_id: str, tty: str, text: str, producer_pid: int, producer_started: str) -> bool:
        """Paste WITHOUT Enter (inert, unsubmitted text); the Enter is a separate, separately authorised ``submit``."""
        return self._two_step(STAGE_SCRIPT, [session_id, tty, str(producer_pid), " ".join(producer_started.split()), text])

    def submit(self, session_id: str, tty: str, producer_pid: int, producer_started: str) -> bool:
        return self._two_step(SUBMIT_SCRIPT, [session_id, tty, str(producer_pid), " ".join(producer_started.split())])

    def _two_step(self, script: str, argv: list[str]) -> bool:
        try:
            res = _run([self.osascript, "-e", script, *argv])
        except (OSError, subprocess.TimeoutExpired):
            return False
        if res.returncode != 0:
            log.warning("iterm step refused: %s", _reason(res))
            return False
        return res.stdout.strip() == "ok"


def process_info(pid: int) -> tuple[str, str, str, str]:
    """``(pgid, tpgid, tty, lstart)`` of a pid, ``("", "", "", "")`` when it is gone."""
    try:
        res = _run(["ps", "-o", "pgid=,tpgid=,tty=,lstart=", "-p", str(pid)])
    except (OSError, subprocess.TimeoutExpired):
        return "", "", "", ""
    parts = res.stdout.split(None, 3) if res.returncode == 0 else []
    if len(parts) != 4:
        return "", "", "", ""
    return parts[0], parts[1], parts[2], " ".join(parts[3].split())


KERN_PROCARGS2 = (1, 49)                                 # CTL_KERN, KERN_PROCARGS2 (macOS)


def parse_procargs2(buf: bytes) -> list[str]:
    """The structured argv out of a ``KERN_PROCARGS2`` buffer: ``int argc``, the exec path, NUL padding, then exactly ``argc``
    NUL-terminated arguments (the environment that follows is never read). ``[]`` for anything malformed."""
    if len(buf) < 8:
        return []
    argc = int.from_bytes(buf[:4], "little", signed=True)
    if not 0 < argc <= 4096:
        return []
    end = buf.find(b"\0", 4)
    if end < 0:
        return []
    pos = end
    while pos < len(buf) and buf[pos] == 0:                  # padding after the exec path
        pos += 1
    out = []
    for _ in range(argc):
        stop = buf.find(b"\0", pos)
        if stop < 0:
            return []                                        # truncated: never a partial proof
        out.append(buf[pos:stop].decode("utf-8", "replace"))
        pos = stop + 1
    return out


def process_argv(pid: int) -> list[str]:
    """Structured argv of a pid, read only and never its environment: the kernel's ``KERN_PROCARGS2`` keeps the argument
    boundaries, which a rendered process-listing command line does not (a prompt argument containing ``--resume <id>`` would
    look like the flag). ``[]`` when the process is gone, not ours, malformed, or the platform has no such source:
    unproven, fail closed."""
    import ctypes
    import sys
    if sys.platform != "darwin" or not isinstance(pid, int) or pid <= 0:
        return []
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(KERN_PROCARGS2[0], KERN_PROCARGS2[1], pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not 0 < size.value <= 1 << 22:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return []
        return parse_procargs2(buf.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return []


def host_matches(hostname: str, local: str | None = None) -> bool:
    """The admitted hostname is this machine (``my-mac.local`` == ``my-mac``); a different host never matches."""
    norm = lambda h: h.strip().lower().removesuffix(".local")
    return bool(hostname) and norm(hostname) == norm(local if local is not None else socket.gethostname())


def check_iterm(api: ITermAPI, pin: ITermPin, info=process_info) -> str:
    """"" when terminal session, tty and producer are exactly the pinned ones, else the typed reason. Read only."""
    bad = valid_pin(pin)
    if bad:
        return bad
    tty, reason = api.probe(pin.iterm_session_id)
    if reason:
        return reason
    if tty != pin.tty:
        return "tty_changed"
    pgid, tpgid, ps_tty, started = info(pin.producer_pid)
    if not started:
        return "producer_gone"
    if not pin.producer_started:
        return "producer_start_unknown"
    if started != " ".join(pin.producer_started.split()):
        return "producer_changed"
    if "/dev/" + ps_tty != pin.tty:
        return "producer_not_on_tty"
    if pgid != tpgid:
        return "producer_not_foreground"
    return ""


class ITermSession:
    """The one existing native session in its iTerm terminal, found by PIN and never created. Same three-method surface as
    ``meta_delivery.TmuxSession`` — the receiver, journal and ledger above it do not change, so there is one injector."""

    def __init__(self, api: ITermAPI, pin: ITermPin, session_id: str, known, info=process_info):
        self.api, self.pin, self.session_id, self._known, self._info = api, pin, session_id, known, info

    def known(self) -> bool:
        try:
            return bool(self._known(self.session_id))
        except OSError:
            return False

    def target(self) -> str | None:
        reason = check_iterm(self.api, self.pin, self._info)
        if reason:
            log.warning("session receiver: iTerm target not as pinned (%s); no injection", reason)
            return None
        return self.pin.iterm_session_id

    def paste(self, target: str, text: str) -> bool:
        if target != self.pin.iterm_session_id or not text:
            return False
        reason = check_iterm(self.api, self.pin, self._info)      # revalidated immediately before the write
        if reason:
            log.warning("session receiver: iTerm target changed before write (%s); no injection", reason)
            return False
        if "\x1b" in text or "\x00" in text:
            return False                                           # text must not carry its own paste terminator
        return self.api.write(self.pin.iterm_session_id, self.pin.tty, text, self.pin.producer_pid, self.pin.producer_started)

    def paste_guarded(self, target: str, text: str, guard) -> str:
        """Two-step injection for an authority that must be asked right before the paste and again right before the Enter.

        ``guard()`` returns "" to allow or a typed reason. Result: ``"submitted"``; ``"refused"`` when nothing was written
        (the guard or the local binding said no BEFORE the paste); ``"uncertain"`` when text may sit unsubmitted in the
        terminal (stage failed, authority/binding refused between paste and Enter, or submit failed)."""
        if target != self.pin.iterm_session_id or not text or "\x1b" in text or "\x00" in text:
            return "refused"
        if guard() or check_iterm(self.api, self.pin, self._info):
            return "refused"
        if not self.api.stage(self.pin.iterm_session_id, self.pin.tty, text, self.pin.producer_pid, self.pin.producer_started):
            return "uncertain"
        reason = guard() or check_iterm(self.api, self.pin, self._info)
        if reason:
            log.warning("session receiver: authority/binding changed between paste and Enter (%s); no Enter", reason)
            return "uncertain"
        if not self.api.submit(self.pin.iterm_session_id, self.pin.tty, self.pin.producer_pid, self.pin.producer_started):
            return "uncertain"
        return "submitted"

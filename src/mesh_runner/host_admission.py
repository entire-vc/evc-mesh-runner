"""One host cap for everything that occupies a live-session slot: card windows, chat windows and editor jobs.

Windows are counted from tmux, as before. An editor job has no window, so it holds a *lease*: a file in
``<interactive root>/.editor-leases`` whose flock the holder keeps for as long as the job runs. The kernel drops the lock
when the holder dies, so a crashed runner never leaves a slot taken: the next count sees an unlocked lease and removes it.

Count, evict and create happen under the same ``.start.lock`` the tui driver already takes for a window start, so a window
start and an editor admission can never both take the last slot. Both directions go through ``count_leases`` /
``make_room`` here.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

LEASE_DIR = ".editor-leases"
LOCK_NAME = ".start.lock"
POLL = 0.05


class AdmissionError(Exception):
    """Base of the reasons an admission did not happen."""


class AdmissionClosed(AdmissionError):
    pass


class AdmissionTimeout(AdmissionError):
    pass


class AdmissionCancelled(AdmissionError):
    pass


@contextlib.contextmanager
def start_lock(root: Path, give_up: Callable[[], bool] | None = None, sleep=time.sleep):
    """The host-wide start lock. With ``give_up`` the lock is polled so a waiter can stop; raises ``AdmissionTimeout``."""
    path = Path(root) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if give_up is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if give_up():
                        raise AdmissionTimeout("start lock") from None
                    sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def count_leases(root: Path) -> int:
    """Live editor leases. Call under the start lock. A lease whose lock is free has no holder and is removed."""
    directory = Path(root) / LEASE_DIR
    live = 0
    try:
        names = sorted(p for p in directory.iterdir() if p.suffix == ".lease")
    except OSError:
        return 0
    for p in names:
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live += 1
                continue
            p.unlink(missing_ok=True)                      # nobody holds it: a dead holder's leftover
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return live


def make_room(tmux, root: Path, cap: int, *, own_key: str = "", read_own_meta, pid_alive, forget, chat_prefix: str,
              log=sys.stderr) -> bool:
    """Room for one more session under ``cap``, counting windows and editor leases together; close the card windows
    idle longest when needed. Call under the start lock. A chat window counts but is never closed here, and a window
    without readable metadata under ``root`` is unknown (another runner's) and never closed either. False when the
    cap is reached and no window of ours is provably idle."""
    listed = tmux.all_windows()
    held = len(listed) + count_leases(root)
    if cap <= 0 or held < cap:
        return True
    windows = [(se, w) for se, w in listed if not w.startswith(chat_prefix)]
    idle = []
    for session, window in windows:
        directory = Path(root) / session[len("mr-"):]
        meta = read_own_meta(directory, window)
        if meta is None:
            continue
        if pid_alive(int(meta.get("busy_pid") or 0)) or (own_key and window == own_key):
            continue
        idle.append((float(meta.get("last_end") or 0), session, window, directory))
    need = held - cap + 1
    if len(idle) < need:
        return False
    for last_end, session, window, directory in sorted(idle)[:need]:
        print(f"INFO make_room: closed idle window agent={session[len('mr-'):]} window={window} "
              f"last_end={last_end:.0f} (host sessions {held}, cap {cap})", file=log, flush=True)
        tmux.kill_window(session, window)
        forget(directory, window)
    return True


class Lease:
    """One held slot. ``release`` is idempotent: the slot is given back exactly once."""

    def __init__(self, path: Path, fd: int):
        self.path, self._fd, self._mu = path, fd, threading.Lock()
        self.released = 0

    def release(self) -> None:
        with self._mu:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
        self.path.unlink(missing_ok=True)
        os.close(fd)                                       # drops the flock
        self.released += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


class HostAdmission:
    """Admission of editor jobs into the host cap shared with card and chat windows (``interactive_max_sessions``)."""

    def __init__(self, root: Path, cap: int, tmux, *, read_own_meta=None, pid_alive=None, forget=None,
                 chat_prefix: str = "chat-", mono: Callable[[], float] = time.monotonic, sleep=time.sleep):
        from mesh_runner import interactive
        self.root, self.cap, self.tmux = Path(root), cap, tmux
        self.mono, self.sleep = mono, sleep
        self._meta = read_own_meta or _read_own_meta
        self._alive = pid_alive or interactive.pid_alive
        self._forget = forget or interactive.forget
        self._chat = chat_prefix
        self._closed = threading.Event()

    def close(self) -> None:
        """Refuse new admissions and wake every waiter; leases already held stay until their holder releases them."""
        self._closed.set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def held(self) -> int:
        with start_lock(self.root):
            return count_leases(self.root)

    def acquire(self, deadline: float, cancel: threading.Event | None = None) -> Lease:
        """A lease before ``deadline`` (monotonic), else ``AdmissionTimeout`` / ``AdmissionCancelled`` / ``AdmissionClosed``."""
        def stop() -> bool:
            return self.mono() >= deadline or self._closed.is_set() or (cancel is not None and cancel.is_set())

        while True:
            self._check(deadline, cancel)
            try:
                with start_lock(self.root, stop, self.sleep):
                    self._check(deadline, cancel)
                    if make_room(self.tmux, self.root, self.cap, read_own_meta=self._meta, pid_alive=self._alive,
                                 forget=self._forget, chat_prefix=self._chat):
                        return self._create()
            except AdmissionTimeout:
                self._check(deadline, cancel)
                raise
            self.sleep(POLL)

    def _check(self, deadline: float, cancel: threading.Event | None) -> None:
        if self._closed.is_set():
            raise AdmissionClosed()
        if cancel is not None and cancel.is_set():
            raise AdmissionCancelled()
        if self.mono() >= deadline:
            raise AdmissionTimeout()

    def _create(self) -> Lease:
        directory = self.root / LEASE_DIR
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{os.getpid()}-{uuid.uuid4().hex}.lease"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        return Lease(path, fd)


def _read_own_meta(directory: Path, key: str) -> dict | None:
    from mesh_runner.interactive import meta_path
    import json
    try:
        data = json.loads(meta_path(directory, key).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

"""Optional external start order for a lane's ordinary work (interface v1, default off).

``queue_order_command`` (argv) receives one JSON object on stdin, ``{"version": 1, "agent",
"candidates": [{id, priority, created_at, version, workspace_id, project_id}]}`` in the runner's
own order, and answers ``{"version": 1, "order": [ids], ...}``. Only ``order`` is used, and only
when it is an exact permutation of the input ids. Anything else (exit != 0, timeout, garbage,
another version, a missing/extra/repeated id) means the runner's own order. A valid optional
``fair_ids`` list identifies fairness positions in the accepted order. Titles, bodies and labels
never leave the runner. It never raises.
"""
from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import threading
import time

log = logging.getLogger("mesh_runner")

VERSION = 1
FIELDS = ("id", "priority", "created_at", "version", "workspace_id", "project_id")
MAX_REPLY = 1 << 20


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_helper(argv: list[str], body: bytes, timeout: float) -> str:
    """One bounded helper run; returns stdout, raises RuntimeError with the reason on failure."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot start: {e.__class__.__name__}") from None
    def feed() -> None:
        try:
            proc.stdin.write(body)
            proc.stdin.close()
        except OSError:                          # the helper closed its stdin early: its exit decides
            pass
    threading.Thread(target=feed, daemon=True).start()
    end = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    fd = proc.stdout.fileno()
    try:
        while True:                              # bounded read: the cap holds while the helper writes
            left = end - time.monotonic()
            if left <= 0:
                _kill_group(proc)
                raise RuntimeError(f"timeout after {timeout:g}s")
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_REPLY:
                _kill_group(proc)
                raise RuntimeError("reply too large")
            chunks.append(chunk)
        proc.wait(timeout=max(end - time.monotonic(), 0.01))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise RuntimeError(f"timeout after {timeout:g}s") from None
    except OSError as e:
        _kill_group(proc)
        raise RuntimeError(f"io error: {e.__class__.__name__}") from None
    except BaseException:
        _kill_group(proc)
        raise
    _kill_group(proc)                            # a descendant that outlived the helper
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}")
    out = b"".join(chunks)
    return out.decode("utf-8", "replace")


def parse_reply(text: str, ids: list[str]) -> list[str]:
    """The ``order`` of a v1 reply; ValueError when it is not an exact permutation of ``ids``."""
    return _parse_reply(text, ids)[0]


def _parse_reply(text: str, ids: list[str]) -> tuple[list[str], frozenset[str] | None]:
    try:
        reply = json.loads(text)
    except ValueError:
        raise ValueError("not JSON") from None
    if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != VERSION:
        raise ValueError("version is not 1")
    order = reply.get("order")
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
        raise ValueError("order is not a list of ids")
    if len(order) != len(ids) or sorted(order) != sorted(ids):
        raise ValueError("order is not a permutation of the input")
    fair = reply.get("fair_ids")
    if isinstance(fair, list) and all(isinstance(i, str) for i in fair):
        fair_ids = frozenset(fair)
        if len(fair_ids) == len(fair) and fair_ids.issubset(ids):
            return order, fair_ids
    return order, None


class QueueOrder:
    def __init__(self, agent: str, argv: list[str], timeout: float = 3.0, cache_seconds: float = 60.0):
        self.agent, self.argv = agent, list(argv)
        self.timeout, self.cache_seconds = timeout, cache_seconds
        self._cache: tuple | None = None          # (key, monotonic time, order, fairness ids)
        self.applied_fair_ids: frozenset[str] | None = None
        self._failing = False
        self._last_first = ""

    def apply(self, tasks: list[dict]) -> list[dict]:
        """``tasks`` in the runner's own order -> the external order, or ``tasks`` itself."""
        self.applied_fair_ids = None
        try:
            return self._apply(tasks)
        except Exception as e:                    # never out of candidates()
            self._fail(f"internal error: {e.__class__.__name__}")
            return tasks

    def _apply(self, tasks: list[dict]) -> list[dict]:
        ids = [t.get("id") for t in tasks]
        if len(tasks) < 2 or not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
            return tasks
        key = (self.agent, tuple(ids), tuple((t.get("priority"), t.get("version")) for t in tasks))
        now = time.monotonic()
        if self._cache and self._cache[0] == key and now - self._cache[1] < self.cache_seconds:
            order, fair_ids = self._cache[2:]
        else:
            body = json.dumps({"version": VERSION, "agent": self.agent,
                               "candidates": [{f: t.get(f) for f in FIELDS} for t in tasks]},
                              separators=(",", ":"), ensure_ascii=False).encode()
            try:
                order, fair_ids = _parse_reply(run_helper(self.argv, body, self.timeout), ids)
            except (RuntimeError, ValueError) as e:
                self._fail(str(e))
                self._cache = (key, now, None, None)  # don't ask a failing helper every tick
                return tasks
            self._cache = (key, now, order, fair_ids)
            if self._failing:
                self._failing = False
                log.info("queue order: %s helper recovered", self.agent)
        if order is None:
            return tasks
        by_id = {t["id"]: t for t in tasks}
        result = [by_id[i] for i in order]
        if order[0] != ids[0]:
            line = f"{order[0][:8]}:{ids[0][:8]}"
            if line != self._last_first:
                self._last_first = line
                log.info("queue order: %s first #%s (own #%s)", self.agent, order[0][:8], ids[0][:8])
        else:
            self._last_first = ""
        self.applied_fair_ids = fair_ids
        return result

    def _fail(self, reason: str) -> None:
        if not self._failing:
            self._failing = True
            log.warning("queue order: %s falling back to the runner's own order: %s", self.agent, reason)

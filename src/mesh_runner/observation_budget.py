"""One monotonic deadline for a whole WAIT observation (every read and every backoff of one wake).

Without it each GET carried its own 30s timeout plus 1s/3s retries (up to ~94s per request), so a
slow Mesh held a lane's tick for minutes and the cause was lost behind ``wake timed out``. The
budget is per thread and per attempt; it only ever shortens a read, it never repeats one.

A failure is described by ``describe``: kind (timeout/transport/parse/http), stage (the endpoint
shape, ids and query removed), elapsed seconds and retry count. No URL query, body or credential.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error

KINDS = ("timeout", "transport", "parse", "http")
_local = threading.local()
# the literal words of the Mesh REST paths the client builds; any other segment is a dynamic value
_WORDS = frozenset((
    "agents api assign by-short-id checkout comments compare-clear dependencies extend heartbeat me "
    "mentionables mentions move projects release report runtime-state seen sessions start-after statuses "
    "subtasks tasks v1 vcs-links workspaces vcs").split())


class Exhausted(Exception):
    """The observation budget is spent; the read was not attempted."""


class Observation:
    def __init__(self, seconds: float):
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.seconds = seconds
        self.retries = 0
        self.stage = ""

    def elapsed(self) -> float:
        return time.monotonic() - self.started


@contextlib.contextmanager
def observe(seconds: float):
    """Open a budget; a nested call keeps the outer (shorter-lived) one."""
    outer = getattr(_local, "obs", None)
    if outer is not None:
        yield outer
        return
    obs = Observation(float(seconds))
    _local.obs = obs
    try:
        yield obs
    finally:
        _local.obs = None


def current() -> Observation | None:
    return getattr(_local, "obs", None)


def stage_of(method: str, path: str) -> str:
    """``GET /tasks/:id/comments`` — the endpoint shape, never ids or a query string."""
    parts = path.split("?", 1)[0].split("/")
    return f"{method} " + "/".join(p if p in _WORDS or not p else ":id" for p in parts)


def cap(timeout: float, method: str = "", path: str = "") -> float:
    """The socket timeout allowed now: ``timeout`` or what is left of the budget."""
    obs = current()
    if obs is None:
        return timeout
    if method:
        obs.stage = stage_of(method, path)
    left = obs.deadline - time.monotonic()
    if left <= 0.05:
        raise Exhausted("observation budget spent")
    return min(timeout, left)


def sleep_allowed(delay: float) -> bool:
    """True when a backoff of ``delay`` still leaves room for the retry after it."""
    obs = current()
    if obs is None:
        return True
    if obs.deadline - time.monotonic() <= delay + 0.05:
        return False
    obs.retries += 1
    return True


def _retime(resp, seconds: float) -> None:
    """Shorten the live socket's timeout to what is left: the one set before the headers arrived does not
    know how much of the budget opening the response used. Best effort on a private attribute path."""
    for holder in (resp, getattr(resp, "fp", None)):
        sock = getattr(getattr(getattr(holder, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(max(seconds, 0.05))
            except OSError:
                pass
            return


def read(resp, max_bytes: int | None = None) -> bytes:
    """Read a response body against the wall deadline, not only the per-recv socket timeout: a peer that
    trickles bytes resets the socket timeout every time, so the deadline is checked between chunks."""
    obs = current()
    if obs is None:
        return resp.read() if max_bytes is None else resp.read(max_bytes)
    out = bytearray()
    while max_bytes is None or len(out) < max_bytes:
        left = obs.deadline - time.monotonic()
        if left <= 0:
            raise Exhausted("observation budget spent while reading a response")
        _retime(resp, left)
        # read1: one recv at most; a buffered read(n) would wait for all n bytes past the deadline
        step = getattr(resp, "read1", resp.read)
        chunk = step(8192 if max_bytes is None else min(8192, max_bytes - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


def classify(exc: BaseException) -> str:
    """timeout/transport/parse/http for a failed read; '' for anything that is not a read failure."""
    kind = getattr(exc, "kind", "")
    if kind in KINDS:
        return kind
    if isinstance(exc, Exhausted):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "transport"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "parse"
    if isinstance(exc, (ConnectionError, socket.gaierror, ssl.SSLError, http.client.HTTPException)):
        return "transport"
    return ""


def describe(exc: BaseException, obs: Observation | None = None) -> str:
    """Safe one-line cause: no query, body or credential ever enters it."""
    obs = obs or current()
    kind = classify(exc) or "other"
    parts = [f"kind={kind}"]
    status = getattr(exc, "status", 0) or getattr(exc, "code", 0)
    if kind == "http" and status:
        parts.append(f"status={status}")
    stage = getattr(exc, "stage", "") or (obs.stage if obs else "")
    if stage:
        parts.append(f"stage={stage}")
    if obs is not None:
        parts += [f"elapsed={obs.elapsed():.1f}s", f"budget={obs.seconds:g}s", f"retries={obs.retries}"]
    return "wake read failed: " + " ".join(parts)

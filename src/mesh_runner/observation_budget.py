"""One monotonic deadline for a whole WAIT observation (every read and every backoff of one wake).

Without it each GET carried its own 30s timeout plus 1s/3s retries (up to ~94s per request), so a
slow Mesh held a lane's tick for minutes and the cause was lost behind ``wake timed out``. The
budget is per thread and per attempt; it only ever shortens a read, it never repeats one.

The socket timeout ``cap`` gives is an idle timeout per recv, and ``read`` checks the wall deadline only once
the body flows. Everything before the body (connect, proxy tunnel, TLS handshake, status line, headers) is
held to the wall limit by ``urlopen``: a watchdog timer shuts the connection's socket when the limit passes.

A failure is described by ``describe``: kind (timeout/transport/parse/http), stage (the endpoint
shape, ids, slugs and query removed), the exception class (with the errno name of an OS error),
elapsed seconds and retry count. No URL query, body, message or credential.
"""
from __future__ import annotations

import contextlib
import errno
import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

KINDS = ("timeout", "transport", "parse", "http")
log = logging.getLogger("mesh_runner")
_local = threading.local()
# the literal words of the Mesh REST and VCS paths the client builds; any other segment is a dynamic value
_WORDS = frozenset((
    "actions agents api assign bridges by-short-id checkout comments compare-clear dependencies extend "
    "github gitlab heartbeat jobs me mentionables mentions merge_requests move pipelines projects pulls "
    "release report repos runs runtime-state seen sessions start-after statuses subtasks tasks trace v1 "
    "vcs vcs-links workspaces").split())


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


# segments that name a project, whatever they spell: GitHub ``repos/<owner>/<repo>``, GitLab and Mesh
# ``projects/<id or url-encoded path>``. A slug equal to a path word (an owner called ``actions``) is still a slug.
_NAMES_AFTER = {"repos": 2, "projects": 1}


def stage_of(method: str, path: str) -> str:
    """``GET /tasks/:id/comments`` — the endpoint shape, never ids, slugs or a query string."""
    out, names = [], 0
    for p in path.split("?", 1)[0].split("/"):
        if names and p:
            out.append(":id")
            names -= 1
            continue
        out.append(p if p in _WORDS or not p else ":id")
        names = _NAMES_AFTER.get(p, 0)
    return f"{method} " + "/".join(out)


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


# -- the head watchdog ----------------------------------------------------------------------------------------
# ``urlopen(req, timeout=t)`` returns only after the last header byte, and ``t`` restarts on every recv: a peer
# or a proxy sending the head byte by byte holds the open for as long as it likes (runner.log, 11.2026: Mesh
# reads at 165s inside a 45s budget). While a guarded open runs, each TCP socket a guarded connection makes is
# registered with the open's guard; a timer at the open's wall limit shuts it down, which ends the recv
# blocked in a proxy tunnel reply, the TLS handshake, the status line or the headers.


def _shut(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass                                    # already closed by the peer or by its reader


class _Guard:
    def __init__(self, obs: Observation, seconds: float):
        self.obs = obs
        self.until = time.monotonic() + seconds
        self.lock = threading.Lock()
        self.socks: list[socket.socket] = []
        self.fired = self.done = False
        self.timer = threading.Timer(seconds, self._fire)
        self.timer.name = "observation-watchdog"
        self.timer.daemon = True

    def left(self) -> float:
        return self.until - time.monotonic()

    def _fire(self) -> None:
        with self.lock:
            if self.done:
                return
            self.fired = True
            for sock in self.socks:
                _shut(sock)

    def track(self, sock: socket.socket) -> None:
        # A duplicate descriptor: TLS takes the connection over by detaching the original socket object, the
        # duplicate keeps reaching it, and shutdown acts on the connection, not on one descriptor.
        try:
            dup = sock.dup()
        except OSError:
            return
        with self.lock:
            if not self.done:
                self.socks.append(dup)
                if self.fired:
                    _shut(dup)          # connected after the limit: the first recv ends at once
                return
        dup.close()

    def close(self) -> None:
        self.timer.cancel()
        with self.lock:
            self.done = True
            socks, self.socks = self.socks, []
        for sock in socks:
            sock.close()                # the reader's own descriptor stays open for the body
        if self.timer.ident is not None:
            self.timer.join()           # no timer thread outlives its open

    def expired(self) -> TimeoutError:
        # a timeout, never ``Exhausted``: the read was attempted and cut, as a socket timeout would cut it
        if self.obs.deadline - time.monotonic() <= 0.05:
            return TimeoutError("observation deadline reached before the response head arrived")
        return TimeoutError("attempt limit reached before the response head arrived")


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
    """``socket.create_connection`` of a guarded connection: inside a guarded open the connect is held to what
    is left of the open's limit and the socket is registered with its watchdog; otherwise the stock call."""
    guard = getattr(_local, "guard", None)
    if guard is not None and isinstance(timeout, (int, float)):
        timeout = max(min(timeout, guard.left()), 0.05)
    sock = socket.create_connection(address, timeout, source_address, **kwargs)
    if guard is not None:
        guard.track(sock)
    return sock


class GuardedConnection:
    """Mixin for an ``http.client`` connection whose socket the watchdog of a guarded open can shut."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_connection


class GuardedHTTPConnection(GuardedConnection, http.client.HTTPConnection):
    pass


class GuardedHTTPSConnection(GuardedConnection, http.client.HTTPSConnection):
    pass


class GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(GuardedHTTPConnection, req)


class GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(GuardedHTTPSConnection, req, context=self._context)


def urlopen(opener, req, timeout: float):
    """``opener(req, timeout=timeout)`` held to a wall limit of ``timeout`` from the connect to the last header
    byte while an observation is open; outside one, the plain call. ``timeout`` is what ``cap`` allowed: the
    attempt limit or the rest of the budget. Only connections made by a guarded connection class can be shut.

    A fired watchdog fails the open whatever came back (a head cut short parses as a complete 200), with a
    ``TimeoutError`` naming the budget or the attempt limit: the fault a socket timeout at that moment raises,
    so the race between the two cannot change the outcome. The body is ``read``'s business."""
    obs = current()
    if obs is None:
        return opener(req, timeout=timeout)
    guard = _Guard(obs, timeout)
    try:
        guard.timer.start()
    except RuntimeError:                # no thread to spare: the read goes unguarded, as before the watchdog
        log.warning("observation watchdog unavailable; read unguarded")
        return opener(req, timeout=timeout)
    outer, _local.guard = getattr(_local, "guard", None), guard
    try:
        resp = opener(req, timeout=timeout)
    except Exception as e:
        if not guard.fired:
            raise
        close = getattr(e, "close", None)          # an HTTPError holds the response
        if close is not None:
            close()
        raise guard.expired() from None
    finally:
        _local.guard = outer
        guard.close()
    if guard.fired:
        resp.close()
        raise guard.expired()
    return resp


# A bare OSError raised mid-read by the socket layer (urllib wraps only the connect): these errnos are
# the network's, so the read failed in the channel. A local OSError (a file, a permission) stays ''.
_NET_ERRNOS = frozenset(getattr(errno, n) for n in (
    "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", "ENETRESET", "ENOTCONN", "EADDRNOTAVAIL",
    "ECONNABORTED", "ECONNRESET", "ECONNREFUSED", "EPIPE") if hasattr(errno, n))


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
    if type(exc) is OSError and exc.errno == errno.ETIMEDOUT:
        return "timeout"
    if type(exc) is OSError and exc.errno in _NET_ERRNOS:
        return "transport"
    return ""


def cause_of(exc: BaseException) -> str:
    """``URLError/ConnectionResetError:ECONNRESET``: the classes and the errno name, never a message."""
    inner = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
    chain = [exc] + ([inner] if isinstance(inner, BaseException) else [])
    names = "/".join(type(e).__name__ for e in chain)
    last = chain[-1]
    if isinstance(last, (socket.gaierror, socket.herror, ssl.SSLError)):
        return names                     # their errno is a resolver or TLS code, not an errno value
    code = getattr(last, "errno", None)
    return f"{names}:{errno.errorcode[code]}" if isinstance(code, int) and code in errno.errorcode else names


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
    cause = getattr(exc, "cause", "") or ("" if getattr(exc, "kind", "") else cause_of(exc))
    if cause:
        parts.append(f"cause={cause}")
    if obs is not None:
        parts += [f"elapsed={obs.elapsed():.1f}s", f"budget={obs.seconds:g}s", f"retries={obs.retries}"]
    return "wake read failed: " + " ".join(parts)

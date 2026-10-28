"""Client of the Agent Chat runner protocol v1 (Mesh Doc ``agent-chat-runner-protocol``).

One method per endpoint of ``/api/v1/runner/chat/…``. The client only speaks the wire; what a
runner does with a turn lives in ``chat_turn.py``. Errors carry Mesh's stable ``error_code``
(callers branch on it, never on the message). A token value (``session_token``) is returned
to the caller and never logged or placed in an error.
"""
from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.parse
import uuid
import urllib.request
from typing import Any

from .mesh import MeshClient, MeshError

PROTOCOL = 1
CLAIM_READ_SLACK = 3        # seconds past the server's own deadline before a silent claim is given up
CLAIM_RETRY_BUDGET = 1.5    # seconds the repeat of a silent claim may add: it asks the server for 1 s
COMMANDS_TIMEOUT = 4        # an order fetch rides the claim loop: a silent one must not hold it for the default 30 s
# the runner must stop working on a turn when Mesh answers with one of these (protocol §5.2)
DROP_CODES = frozenset({"chat_stale_claim", "chat_lease_expired", "chat_turn_terminal",
                        "chat_not_claim_holder", "chat_turn_not_found"})


_PHASES = threading.local()      # per thread: when each stage of the current request finished


class _TimedHTTPS(http.client.HTTPSConnection):
    """Marks connect (TCP+TLS), send and first byte, so a silent read timeout says which stage stalled."""

    def _mark(self, stage: str) -> None:
        ph = getattr(_PHASES, "d", None)
        if ph is not None:
            ph[stage] = time.monotonic() - ph["t0"]

    def connect(self):
        super().connect()
        self._mark("connect")

    def send(self, data):
        super().send(data)
        self._mark("sent")

    def getresponse(self):
        resp = super().getresponse()
        self._mark("headers")
        return resp


class _TimedHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_TimedHTTPS, req)


def _timed_open(req, timeout=None):
    _PHASES.d = {"t0": time.monotonic()}
    return urllib.request.build_opener(_TimedHandler()).open(req, timeout=timeout)


def _phase_note() -> str:
    ph = getattr(_PHASES, "d", None) or {}
    done = ", ".join(f"{k}={v:.2f}s" for k, v in ph.items() if k != "t0")
    return f" [phases: {done or 'none reached'}]"


class ChatError(Exception):
    def __init__(self, status: int, error_code: str, message: str = "", context: dict | None = None):
        super().__init__(f"{status} {error_code}: {message}"[:300])
        self.status, self.error_code, self.context = status, error_code, context or {}

    @property
    def drop_turn(self) -> bool:
        """Mesh no longer holds this turn for us: stop, discard buffered events, do not retry."""
        return self.error_code in DROP_CODES

    @property
    def transient(self) -> bool:
        return self.status == 0 or self.status >= 500 or self.status == 429


def _wrap(e: MeshError) -> ChatError:
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    code = str(err.get("error_code") or "") if isinstance(err, dict) else ""
    ctx = err.get("context") if isinstance(err, dict) and isinstance(err.get("context"), dict) else {}
    msg = str(err.get("message") or "")[:200] if isinstance(err, dict) else ""
    if e.status == 0 and not msg:
        msg = str(e)[:200]      # which socket error it was (reset, timeout, TLS), not just "transport"
    return ChatError(e.status, code or ("transport" if e.status == 0 else f"http_{e.status}"), msg, ctx)


def load_runner_id(state_dir) -> str:
    """The stable id of this runner instance (``X-Chat-Runner-Id``): kept in ``chat-runner-id`` so a
    restart keeps the identity the turn leases are fenced against."""
    path = os.path.join(os.path.expanduser(str(state_dir)), "chat-runner-id")
    try:
        with open(path) as fh:
            rid = str(uuid.UUID(fh.read().strip()))
        os.chmod(path, 0o600)
        return rid
    except (OSError, ValueError):
        pass
    rid = str(uuid.uuid4())
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(rid + "\n")
    os.replace(tmp, path)
    return rid


def load_runner_credential(state_dir, register, agent: str = "") -> tuple[str, str]:
    """The registered runner of this machine: ``(runner_id, token)`` from ``chat-runner.json`` (0600),
    registered once through ``register()`` (POST /runner/chat/runners; the token is in that answer
    only). Mesh hands a runner its orders (stop_session, delete_transcript) only against this
    credential, so without it a deleted chat's transcript would stay on disk. The token is bound to the
    key that registered it (Mesh answers 403 ``chat_runner_forbidden`` to another agent's key), so a
    runner serving several agents keeps one file per agent: ``chat-runner-<agent>.json``."""
    import json
    name = "chat-runner-%s.json" % urllib.parse.quote(agent, safe="") if agent else "chat-runner.json"
    path = os.path.join(os.path.expanduser(str(state_dir)), name)
    try:
        with open(path) as fh:
            cur = json.load(fh)
        rid, token = str(uuid.UUID(str(cur["runner_id"]))), str(cur["token"])
        if token:
            os.chmod(path, 0o600)
            return rid, token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    got = register()
    rid, token = str(uuid.UUID(str(got["runner_id"]))), str(got["token"])
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"runner_id": rid, "token": token}, fh)
    os.replace(tmp, path)
    return rid, token


class ChatClient:
    """``credential`` is the API key of the agent the runner serves, as Mesh's runner chat routes
    take it (``X-Agent-Key``; ``Authorization: Bearer`` is a 401 on production), next to
    ``X-Chat-Runner-Id``. Never shown by ``repr``."""

    def __init__(self, base_url: str, credential: str, timeout: int = 30, opener=None,
                 runner_id: str = "", runner_token: str = ""):
        self._runner_id = runner_id or str(uuid.uuid4())
        self._runner_token = runner_token
        if not base_url.startswith("https://"):
            raise ValueError("the runner credential is sent only over https")
        self._credential = credential
        self._local = threading.local()                  # per-thread extra headers: calls run concurrently
        self._injected = opener is not None
        self._conn_factory = http.client.HTTPSConnection     # tests swap it for a plain-HTTP double
        self._claim_conn = None
        real = opener or _timed_open

        def open_with_headers(req, timeout=None):
            req.add_header("X-Agent-Key", self._credential)
            req.add_header("X-Chat-Runner-Id", self._runner_id)
            if self._runner_token:
                req.add_header("X-Runner-Token", self._runner_token)
            req.add_header("X-Chat-Protocol", str(PROTOCOL))
            for k, v in getattr(self._local, "headers", {}).items():
                req.add_header(k, v)
            return real(req, timeout=timeout)
        base = base_url.rstrip("/") + "/api/v1/runner/chat"
        self._mesh = MeshClient(base_url, "", timeout=timeout, opener=open_with_headers)
        self._mesh.base = base
        # the long-poll has its own client (and timeout); only the claim loop uses it
        self._poll = MeshClient(base_url, "", timeout=timeout, opener=open_with_headers)
        self._poll.base = base

    def __repr__(self) -> str:
        return f"ChatClient({self._mesh.base!r})"

    def _call(self, method: str, path: str, body: Any = None, query: dict | None = None,
              headers: dict | None = None, long: float = 0.0, timeout: float | None = None) -> Any:
        client = self._mesh
        if long:
            client = self._poll
            client.timeout = long
        self._local.headers = dict(headers or {})
        try:
            # a long-poll GET is repeated by the loop in the service, not by the transport retry
            return client._request(method, path, body, query, timeout=timeout)
        except MeshError as e:
            err = _wrap(e)
            if e.status == 0 and not getattr(self, "_injected", False):
                err.args = ((err.args[0] if err.args else "")[:240] + _phase_note(),)
            raise err from None
        finally:
            self._local.headers = {}

    def register_runner(self, name: str) -> dict:
        """POST /runners: ``{runner_id, token, …}``; the token is shown once."""
        return self._call("POST", "/runners", {"name": name[:80]}) or {}

    # -- claim ---------------------------------------------------------------------
    def claim(self, agents: list[str], timeout: int = 30, max_turns: int = 1) -> dict | None:
        """Long-poll. ``None`` on 204/empty; otherwise ``{"turns": [...], "commands": [...]}``."""
        query = {"agents": ",".join(agents), "timeout": int(timeout), "max": int(max_turns)}
        if self._injected:
            return self._call("GET", "/claim", query=query, long=timeout + CLAIM_READ_SLACK) or None
        # One kept-alive connection for the claim loop: a new TCP/TLS flow every ~3 s through the
        # provider's NAT silently lost ~1% of the polls (request sent, no answer, never seen by Mesh).
        # A poll that got no answer is repeated once on a fresh connection; only a second silence is an error.
        # The repeat asks for the server's shortest wait and gets only what is left of the budget,
        # so two silences cost timeout + CLAIM_READ_SLACK + CLAIM_RETRY_BUDGET, not two full reads.
        last: MeshError | None = None
        took: list[str] = []
        deadline = time.monotonic() + timeout + CLAIM_READ_SLACK + CLAIM_RETRY_BUDGET
        read = float(timeout + CLAIM_READ_SLACK)
        for _ in range(2):
            t0 = time.monotonic()
            try:
                return self._claim_once(query, read) or None
            except MeshError as e:
                if e.status != 0 or getattr(e, "answered", False):      # an answer, however odd, is not silence
                    raise _wrap(e) from None
                last = e
                took.append(f"{time.monotonic() - t0:.1f}s")
                query = {**query, "timeout": 1}
                read = max(deadline - time.monotonic(), 0.5)
        err = _wrap(last)
        err.args = ((err.args[0] if err.args else "")[:240] + f" [claim attempts: {', '.join(took)}]",)
        raise err from None

    def _claim_once(self, query: dict, read_timeout: float) -> Any:
        import json
        host = urllib.parse.urlsplit(self._mesh.base)
        path = host.path + "/claim?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "X-Agent-Key": self._credential,
                   "X-Chat-Runner-Id": self._runner_id, "X-Chat-Protocol": str(PROTOCOL)}
        if self._runner_token:
            headers["X-Runner-Token"] = self._runner_token
        conn = getattr(self, "_claim_conn", None)
        try:
            if conn is None:
                conn = self._claim_conn = self._conn_factory(host.netloc, timeout=read_timeout)
            conn.timeout = read_timeout
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.will_close:
                conn.close()
        except (OSError, http.client.HTTPException) as e:
            self._drop_claim_conn()
            raise MeshError(0, f"{type(e).__name__}: {str(e)[:200]}") from None
        if resp.status >= 400:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode(errors="replace")[:500]
            msg = body.get("message") if isinstance(body, dict) else str(body)[:200]
            raise MeshError(resp.status, msg or resp.reason, body, endpoint="GET /claim")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            err = MeshError(0, "response is not JSON")
            err.answered = True
            raise err from None

    def _drop_claim_conn(self) -> None:
        conn, self._claim_conn = getattr(self, "_claim_conn", None), None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def active(self) -> list[dict]:
        res = self._call("GET", "/turns/active") or {}
        return list(res.get("turns") or [])

    def commands(self, agents: list[str]) -> list[dict]:
        query = {"agents": ",".join(agents)}
        if self._runner_token:
            query["runner_id"] = self._runner_id
        res = self._call("GET", "/commands", query=query, timeout=COMMANDS_TIMEOUT) or {}
        return list(res.get("commands") or [])

    # -- one turn ------------------------------------------------------------------
    def start(self, turn_id: str, claim_token: str) -> dict:
        return self._call("POST", f"/turns/{turn_id}/start", {"claim_token": claim_token}) or {}

    def lease(self, turn_id: str, claim_token: str) -> dict:
        return self._call("POST", f"/turns/{turn_id}/lease", {"claim_token": claim_token}) or {}

    def release(self, turn_id: str, claim_token: str, reason: str) -> dict:
        return self._call("POST", f"/turns/{turn_id}/release",
                          {"claim_token": claim_token, "reason": reason[:200]}) or {}

    def events(self, turn_id: str, claim_token: str, events: list[dict]) -> dict:
        return self._call("POST", f"/turns/{turn_id}/events",
                          {"claim_token": claim_token, "events": events}) or {}

    def session_token(self, turn_id: str, claim_token: str, epoch: int) -> dict:
        """The only channel of the token value: the caller writes it to the session's 0600 file."""
        return self._call("POST", f"/turns/{turn_id}/session-token", {"claim_token": claim_token},
                          headers={"X-Chat-Epoch": str(int(epoch))}) or {}

    def download_url(self, turn_id: str, attachment_id: str) -> dict:
        return self._call("GET", f"/turns/{turn_id}/attachments/{urllib.parse.quote(attachment_id)}/download-url") or {}

    # -- commands and history ------------------------------------------------------
    def ack(self, command_id: str, result: str, detail: str = "") -> dict:
        return self._call("POST", f"/commands/{command_id}/ack", {"result": result, "detail": detail[:200]}) or {}

    def history(self, thread_id: str, limit: int = 20, before: str = "") -> dict:
        return self._call("GET", f"/threads/{thread_id}/history",
                          query={"limit": limit, "before": before or None}) or {}


def registered_client(base_url: str, credential: str, state_dir, runner_id: str, name: str,
                      opener=None, agent: str = "") -> "ChatClient":
    """The chat client of this runner: registered (orders arrive) when Mesh allows it, otherwise the
    plain one (turns still flow; stop/delete orders do not) and a warning without the token."""
    import logging
    client = ChatClient(base_url, credential, runner_id=runner_id, opener=opener)
    try:
        rid, token = load_runner_credential(state_dir, lambda: client.register_runner(name), agent)
    except (ChatError, OSError, KeyError, ValueError, TypeError) as e:
        logging.getLogger("mesh_runner").warning(
            "chat runner registration failed (stop/delete orders will not arrive): %s",
            getattr(e, "error_code", type(e).__name__))
        return client
    return ChatClient(base_url, credential, runner_id=rid, runner_token=token, opener=opener)

"""Service endpoint of the Mesh extension: ``/healthz``, ``/mesh/install``, ``/mesh/events``.

Mesh installs an external extension with a signed handshake and then delivers events to it. Every
request carries ``X-Mesh-Timestamp`` and ``X-Mesh-Signature`` = hex HMAC-SHA256 of
``"{timestamp}.{body}"``. The signing secret arrives once, in the install body, and is kept in
``state_dir`` with mode 600. It is never logged and never echoed.

Trust model: the install signature is made with the secret that travels in the same body, so on its
own it only proves the body is intact. The install is authorised by a credential Mesh and the operator
share in advance: ``expected_secret`` is the workspace secret ``EXT_AGENT_RUNNER_SIGNING_SECRET``
(Mesh reuses an existing one), set in Mesh and given to the runner by file or env name; an install
carrying any other secret is refused (403). Only with ``trust_first_install`` does the first install
win without it (a private link where nothing else reaches the port). ``workspace_id`` must match as
well. After install every request is signed with the stored secret; a different secret is refused
with 409 until the operator removes the secret file or sets ``allow_rekey``.

Events only wake the poll loop of the runner; they carry no work of their own.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

log = logging.getLogger("mesh_runner")

SECRET_FILE = "extension-signing-secret.json"
WAKE_EVENTS = frozenset({"task.assigned", "task.updated", "comment.created"})
MAX_BODY = 1 << 20
TOLERANCE_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def signature_ok(secret: str, timestamp: str, signature: str, body: bytes, now: float,
                 tolerance: float = TOLERANCE_SECONDS) -> bool:
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - sent) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), (signature or "").strip().lower())


class SecretStore:
    """The install record: signing secret + the workspace and extension it was issued for."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / SECRET_FILE
        self.lock = threading.Lock()

    def load(self) -> dict | None:
        try:
            rec = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) and rec.get("signing_secret") else None

    def save(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, self.path)


class ExtensionToken:
    """The extension actor token (``ext_...``) of this runner, read from a file by name.

    The value is never printed or logged. ``rotate`` asks Mesh for a new token
    (``POST /ext/me/token/rotate``; the old one keeps working for the grace period) and replaces the
    file atomically with mode 600. Without a file the token comes from an environment variable and
    cannot be rotated.
    """

    def __init__(self, file: Path | None = None, env: str = ""):
        self.file, self.env = file, env

    def __repr__(self) -> str:
        return "ExtensionToken(<hidden>)"

    def __call__(self) -> str:
        value = (self.file.read_text() if self.file else os.environ.get(self.env, "")).strip()
        if not value:
            raise RuntimeError("extension token is empty")
        return value

    def rotate(self, client, grace_seconds: int = 300) -> None:
        if not self.file:
            raise RuntimeError("an environment token cannot be rotated; use token_file")
        res = client.request("POST", "/ext/me/token/rotate", {"grace_seconds": grace_seconds})
        token = res.get("extension_token") if isinstance(res, dict) else None
        if not isinstance(token, str) or not token.startswith("ext_"):
            raise RuntimeError("rotation answered without a token")
        tmp = self.file.with_name(self.file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        os.replace(tmp, self.file)


def rotate_token(token: ExtensionToken, client) -> bool:
    """One rotation step of the runner's token loop; a failure is logged and never raised.

    The current token stays in the file on any failure. The server's message is not logged: only the
    status, so nothing it echoes can end up in the log.
    """
    from .mesh import MeshError
    try:
        token.rotate(client)
    except MeshError as e:
        if e.status == 403:
            log.error("extension token rotation refused (HTTP 403): the installed extension has no consented "
                      "capability ext_token:self_rotate; update agent-runner in Mesh and accept its consent, "
                      "or set rotate_days = 0. The current token stays in use")
        else:
            log.warning("extension token rotation failed: HTTP %s", e.status)
        return False
    except (RuntimeError, OSError) as e:
        log.warning("extension token rotation failed: %s", type(e).__name__)
        return False
    log.info("extension token rotated")
    return True


class ExtensionService:
    def __init__(self, state_dir: Path, wake: Callable[[], None], workspace_id: str,
                 expected_secret: str = "", trust_first_install: bool = False,
                 extension_id: str = "agent-runner", allow_rekey: bool = False, tolerance: float = TOLERANCE_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.store = SecretStore(state_dir)
        self.wake, self.extension_id, self.workspace_id = wake, extension_id, workspace_id
        self.allow_rekey, self.tolerance, self.clock = allow_rekey, tolerance, clock
        self.expected_secret, self.trust_first_install = expected_secret, trust_first_install
        self.server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ handlers; each returns (status, json)
    def healthz(self) -> tuple[int, dict]:
        return 200, {"status": "ok", "installed": self.store.load() is not None}

    def install(self, headers, body: bytes) -> tuple[int, dict]:
        ts, sig = headers.get("X-Mesh-Timestamp", ""), headers.get("X-Mesh-Signature", "")
        try:
            req = json.loads(body)
        except ValueError:
            return 400, {"error": "invalid json"}
        if not isinstance(req, dict):
            return 400, {"error": "invalid json"}
        secret = req.get("signing_secret")
        if not isinstance(secret, str) or len(secret) < 16:
            return 400, {"error": "signing_secret missing"}
        if req.get("extension_id") != self.extension_id:
            return 400, {"error": "unknown extension"}
        sent_ws = str(req.get("workspace_id") or "")
        if not self.workspace_id or not hmac.compare_digest(sent_ws.lower(), self.workspace_id.lower()):
            return 403, {"error": "workspace not served here"}
        if self.expected_secret:
            if not hmac.compare_digest(self.expected_secret, secret):
                return 403, {"error": "install not authorised"}
        elif not self.trust_first_install:
            return 403, {"error": "install not authorised"}
        with self.store.lock:
            have = self.store.load()
            if have and hmac.compare_digest(have["signing_secret"], secret):
                # a repeated handshake: same secret, so the stored one verifies it
                if not signature_ok(secret, ts, sig, body, self.clock(), self.tolerance):
                    return 401, {"error": "bad signature"}
                return 200, {"status": "installed", "idempotent": True}
            # first install, or a new secret: the signature can only be checked with the secret in the body
            if not signature_ok(secret, ts, sig, body, self.clock(), self.tolerance):
                return 401, {"error": "bad signature"}
            if have and not self.allow_rekey:
                return 409, {"error": "already installed with another secret"}
            self.store.save({"signing_secret": secret, "workspace_id": str(req.get("workspace_id") or ""),
                             "version": str(req.get("version") or ""), "installed_at": int(self.clock())})
        log.info("extension installed for workspace %s", str(req.get("workspace_id") or "")[:8])
        return 200, {"status": "installed"}

    def events(self, headers, body: bytes) -> tuple[int, dict]:
        have = self.store.load()
        if not have:
            return 403, {"error": "not installed"}
        if not signature_ok(have["signing_secret"], headers.get("X-Mesh-Timestamp", ""),
                            headers.get("X-Mesh-Signature", ""), body, self.clock(), self.tolerance):
            return 401, {"error": "bad signature"}
        try:
            event = json.loads(body)
        except ValueError:
            return 400, {"error": "invalid json"}
        # Mesh sends the event type in the X-Mesh-Event header; a type in the body is only a fallback
        kind = headers.get("X-Mesh-Event") or (event.get("event") or event.get("type") if isinstance(event, dict) else None)
        if kind in WAKE_EVENTS:
            self.wake()
            return 202, {"status": "wake"}
        return 202, {"status": "ignored"}

    # ------------------------------------------------------------------ server
    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):          # never the request line: no headers, no bodies
                pass

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/healthz":
                    self._send(*svc.healthz())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                handler = {"/mesh/install": svc.install, "/mesh/events": svc.events}.get(self.path)
                if handler is None:
                    return self._send(404, {"error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._send(400, {"error": "bad length"})
                if length < 0 or length > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                body = self.rfile.read(length)
                try:
                    self._send(*handler(self.headers, body))
                except Exception as e:
                    log.warning("extension endpoint failed: %s", type(e).__name__)
                    self._send(500, {"error": "internal"})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="extension-service", daemon=True).start()
        return self.server

    def shutdown(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

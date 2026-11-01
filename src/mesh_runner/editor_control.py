"""Operator lifecycle of the tgpeek editor bridge inside ONE running runner: connect, reconfigure, rollback, disable.

There is no new listener and no new daemon: the routes ``/operator/editor/<action>`` live on the extension listener that
already exists, and the controller is the single owner of ``ExtensionService.editor``. The rest of the runner (card and chat
lanes, leases, the host cap, mappings) is never touched, and the runner process is never restarted or signalled.

Trust boundary
  * Off by default: without ``[tgpeek_editor_control] secret_file`` the routes answer ``operator_control_disabled``.
  * A request is signed with its own HMAC secret (file, mode 600, not the Mesh signing secret): ``HMAC(secret,
    "{timestamp}.{path}\\n{body}")``. The body carries a one-time ``request_id`` (replays are refused).
  * Only a loopback peer is served. A ``JOB-TOKEN`` (the CI job token that reaches the editor-job routes) is refused here:
    it carries no administrative right.

Semantics
  * Every mutation names the binding it expects (``expected_generation``, the process identity ``expected_pid`` +
    ``expected_started_at`` + ``expected_instance``, and ``expected_digest`` of the applied config); a stale or wrong one, including a request signed
    for a previous runner process, is ``409 stale_binding``. Nothing is guessed from the disk.
  * Stopping is: admission closed atomically, queued work woken, running work cancelled, a bounded drain, then a residue check
    (no live job, no harness process group, no scratch directory). Timeout or residue is a refusal, never a success; the old
    bridge then stays attached and closed (every submit answers 503) until a later call drains it.
  * The new bridge is built only after the old one is clean, over the same durable ledger: attempts and budgets survive.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from .editor_job import EditorBridge, EditorConfig, build_bridge, canonical, parse_config
from .extension_service import signature_ok

log = logging.getLogger("mesh_runner")

PREFIX = "/operator/editor/"
ACTIONS = ("status", "connect", "reconfigure", "rollback", "disable")
MIN_SECRET = 32
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
MAX_SEEN = 512


def config_digest(cfg: EditorConfig | None) -> str:
    """sha256 of the effective configuration (paths and digests only; the config holds no secret value)."""
    if cfg is None:
        return ""

    def plain(v):
        if isinstance(v, (set, frozenset)):
            return sorted(plain(x) for x in v)
        if isinstance(v, (tuple, list)):
            return [plain(x) for x in v]
        if isinstance(v, Path):
            return str(v)
        return v
    return hashlib.sha256(canonical({f.name: plain(getattr(cfg, f.name)) for f in dataclasses.fields(cfg)})).hexdigest()


def sign_request(secret: str, timestamp: str, path: str, body: bytes) -> str:
    from .extension_service import sign
    return sign(secret, timestamp, path.encode() + b"\n" + body)


class Refused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class EditorController:
    def __init__(self, service, state_dir: Path, secret_file: str, load_section: Callable[[], dict],
                 build: Callable[..., EditorBridge] = build_bridge,
                 admission: Callable[[], object] | None = None, source: str = "",
                 drain_timeout: float | None = None, clock: Callable[[], float] = time.time):
        self.service, self.state_dir, self.secret_file = service, Path(state_dir), secret_file
        self.load_section, self.build, self.admission, self.source = load_section, build, admission, source
        self.drain_timeout, self.clock = drain_timeout, clock
        self.lock = threading.Lock()                    # one control operation at a time
        self.generation = 0
        self.applied: EditorConfig | None = None
        self.previous: EditorConfig | None = None
        self.seen: dict[str, float] = {}
        self.pid = os.getpid()
        self.instance = secrets.token_hex(16)           # random per process: no restart, however quick, can repeat it
        self.started_at = int(clock())                  # with the pid: the identity of the process the binding lives in

    # ------------------------------------------------------------------------------------------ startup binding
    def adopt(self, bridge: EditorBridge | None, cfg: EditorConfig | None) -> None:
        """Record the bridge the runner built from the config at startup as generation 1 (0 when there is none)."""
        with self.lock:
            self.service.editor, self.applied = bridge, cfg
            self.generation = 1 if bridge is not None else 0

    # ------------------------------------------------------------------------------------------------ readback
    def _disk(self) -> tuple[EditorConfig | None, str]:
        try:
            cfg = parse_config(self.load_section(), self.state_dir)
        except (ValueError, OSError):
            return None, "invalid"
        return cfg, ("enabled" if cfg is not None else "disabled")

    def readback(self) -> dict:
        bridge = self.service.editor
        disk, disk_state = self._disk()
        applied, on_disk = config_digest(self.applied), config_digest(disk)
        if bridge is None:
            state = "disabled"
        else:
            state = "closed" if bridge.closed else "active"
        return {"state": state, "generation": self.generation, "pid": self.pid, "started_at": self.started_at, "instance": self.instance, "source": self.source,
                "applied_digest": applied, "disk_digest": on_disk, "disk_state": disk_state,
                "applied_matches_disk": bool(applied) and applied == on_disk,
                "previous_digest": config_digest(self.previous),
                "executor": ("configured" if self.applied is not None and self.applied.argv else "unprovisioned")
                if self.applied is not None else "none"}

    # --------------------------------------------------------------------------------------------- operations
    def _expect(self, req: dict, need_digest: bool) -> None:
        gen, pid, started = req.get("expected_generation"), req.get("expected_pid"), req.get("expected_started_at")
        inst = req.get("expected_instance")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (gen, pid, started)) or not isinstance(inst, str):
            raise Refused(400, "expected_generation_required")
        # the generation restarts at 1 with every process: pid, start time and a random instance id pin the binding to THIS process
        if (gen != self.generation or pid != self.pid or started != self.started_at
                or not hmac.compare_digest(inst, self.instance)):
            raise Refused(409, "stale_binding")
        if need_digest:
            want = req.get("expected_digest")
            if not isinstance(want, str) or not want:
                raise Refused(400, "expected_digest_required")
            if not hmac.compare_digest(want, config_digest(self.applied)):
                raise Refused(409, "stale_binding")

    def _prepare(self) -> EditorConfig:
        try:
            cfg = parse_config(self.load_section(), self.state_dir)
        except (ValueError, OSError):
            raise Refused(422, "config_invalid")
        if cfg is None:
            raise Refused(422, "config_disabled_on_disk")
        return cfg

    def _stop(self) -> None:
        """Close the attached bridge and prove it clean; on failure it stays attached and closed."""
        bridge = self.service.editor
        if bridge is None:
            return
        kw = {} if self.drain_timeout is None else {"timeout": self.drain_timeout}
        if not bridge.close(**kw):
            raise Refused(504, "drain_timeout")
        if bridge.residue():
            raise Refused(500, "cleanup_failed")
        try:
            bridge.ledger.db.close()
        except Exception:
            pass
        self.service.editor = None
        if self.applied is not None:
            self.previous = self.applied                 # the rollback target, kept even if the next attach fails
        self.applied = None
        self.generation += 1

    def _attach(self, cfg: EditorConfig) -> None:
        try:
            adm = self.admission() if self.admission else None
            bridge = self.build(cfg, admission=adm)
        except Exception as e:
            log.warning("editor attach failed: %s", type(e).__name__)
            raise Refused(500, "build_failed")
        self.service.editor, self.applied = bridge, cfg
        self.generation += 1

    def connect(self, req: dict) -> dict:
        self._expect(req, need_digest=False)
        if self.service.editor is not None:
            raise Refused(409, "already_connected")
        cfg = self._prepare()
        self._attach(cfg)
        return self.readback()

    def reconfigure(self, req: dict, target: EditorConfig | None = None) -> dict:
        self._expect(req, need_digest=self.applied is not None)
        cfg = target or self._prepare()
        if self.service.editor is not None and not self.service.editor.closed and config_digest(cfg) == config_digest(self.applied):
            return dict(self.readback(), unchanged=True)      # idempotent: nothing is stopped
        self._stop()
        self._attach(cfg)
        return self.readback()

    def rollback(self, req: dict) -> dict:
        if self.previous is None:
            self._expect(req, need_digest=self.applied is not None)
            raise Refused(409, "no_previous_config")
        return self.reconfigure(req, target=self.previous)

    def disable(self, req: dict) -> dict:
        self._expect(req, need_digest=self.applied is not None)
        if self.service.editor is None:
            return dict(self.readback(), unchanged=True)
        self._stop()
        return self.readback()

    # ------------------------------------------------------------------------------------------------- http
    def _secret(self) -> str | None:
        if not self.secret_file:
            return None
        try:
            st = os.stat(self.secret_file)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                return None
            value = Path(self.secret_file).read_text().strip()
        except OSError:
            return None
        return value if len(value) >= MIN_SECRET else None

    def _fresh(self, request_id: str, now: float, tolerance: float) -> bool:
        for k in [k for k, t in self.seen.items() if now - t > tolerance]:
            del self.seen[k]
        if request_id in self.seen or len(self.seen) >= MAX_SEEN:
            return False
        self.seen[request_id] = now
        return True

    def handle(self, method: str, path: str, headers, body: bytes, peer: str = "") -> tuple[int, dict]:
        secret = self._secret()
        if secret is None:
            return 403, {"error": "operator_control_disabled"}
        if peer not in LOOPBACK:
            return 403, {"error": "loopback_only"}
        if headers.get("JOB-TOKEN") is not None:
            return 403, {"error": "job_token_not_accepted"}
        action = path[len(PREFIX):]
        if method != "POST" or action not in ACTIONS:
            return 404, {"error": "not_found"}
        now = self.clock()
        if not signature_ok(secret, headers.get("X-Mesh-Timestamp", ""), headers.get("X-Operator-Signature", ""),
                            path.encode() + b"\n" + body, now):
            return 401, {"error": "bad_signature"}
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "invalid_json"}
        rid = req.get("request_id") if isinstance(req, dict) else None
        if not isinstance(rid, str) or not 8 <= len(rid) <= 64:
            return 400, {"error": "request_id_required"}
        with self.lock:
            if not self._fresh(rid, now, 300.0):
                return 409, {"error": "replayed"}
            try:
                if action == "status":
                    return 200, self.readback()
                return 200, getattr(self, action)(req)
            except Refused as e:
                out = {"error": e.code}
                if e.code not in ("expected_generation_required", "expected_digest_required"):
                    out["readback"] = self.readback()
                log.info("editor control %s refused: %s", action, e.code)
                return e.status, out

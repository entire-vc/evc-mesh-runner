"""Strict existing-session metadata delivery (Mesh doc runner-existing-session-metadata-v1).

One bounded TEXT message, capability ``readonly_metadata``, goes into ONE already running native session
that this runner's host admitted. Never a new session: a missing session, a wrong host or session, a stale
admission revision is a typed failure with zero injection and zero session creation (the chat_host.make_plan
fallback that starts a fresh session is deliberately not used here).

A delivery is journalled BEFORE the paste (``injecting``). A crash after that point leaves an ``injecting``
record, which is never pasted again: it becomes ``uncertain`` and fails closed unless the producer's
acknowledgment shows up. ``received`` = bytes pasted into the pinned session; ``acknowledged`` = the producer
answered with this delivery/request id and its own hostname and session.

Route names below are provisional until the backend freezes them on the contract doc.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.request
import uuid
from pathlib import Path

from .interactive import Tmux
from .mesh import MeshClient, MeshError

log = logging.getLogger("mesh_runner.meta_delivery")

CAPABILITY = "readonly_metadata"
MAX_TEXT_BYTES = 16 * 1024
IDENTITY_FIELDS = ("workspace_id", "agent_id", "runner_id", "host_id", "hostname", "session_id")
REQUEST_FIELDS = {"delivery_id", "request_id", "target", "expected_admission_revision", "capability",
                  "text", "text_sha256", "created_at"}
ERR_SHAPE, ERR_FORBIDDEN, ERR_TARGET, ERR_REVISION = "bad_request", "forbidden_field", "wrong_target", "stale_revision"
ERR_SESSION, ERR_CONFLICT, ERR_UNCERTAIN, ERR_PASTE = ("session_missing", "request_conflict", "delivery_uncertain",
                                                       "paste_failed")


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_uuid(value) -> bool:
    """Canonical lowercase UUID text only: ids reach file names and URL paths, so nothing else passes."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Identity:
    """This receiver's own admitted identity: all six fields come from local admission config and the
    senders it admits are named explicitly; a hostname alone never authorizes anything. The admission
    revision is assigned by Mesh at registration, never configured."""

    def __init__(self, allowed_senders, **fields: str):
        if set(fields) != set(IDENTITY_FIELDS) or not all(fields.values()):
            raise ValueError("metadata receiver needs every identity field")
        if (not isinstance(allowed_senders, (list, tuple)) or len(allowed_senders) > 32
                or not all(is_uuid(a) for a in allowed_senders)):
            raise ValueError("allowed_sender_agent_ids must be a list of at most 32 agent UUIDs")
        self.fields, self.allowed_senders = dict(fields), list(allowed_senders)

    def registration(self) -> dict:
        """Frozen body of POST /runner/existing-session/receivers; runner_id travels in X-Runner-Token."""
        f = self.fields
        return {"host_id": f["host_id"], "hostname": f["hostname"], "session_id": f["session_id"],
                "capability": CAPABILITY, "allowed_sender_agent_ids": list(self.allowed_senders)}


class Journal:
    """Durable per-request record, one 0600 file each, written atomically and fsynced."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, request_id: str) -> Path:
        if not is_uuid(request_id):
            raise ValueError("journal key must be a canonical UUID")
        return self.dir / f"{request_id}.json"

    def get(self, request_id: str) -> dict | None:
        try:
            data = json.loads(self._path(request_id).read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def put(self, request_id: str, record: dict) -> None:
        tmp = self._path(request_id).with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self._path(request_id))

    def open_injections(self) -> list[dict]:
        return [r for p in sorted(self.dir.glob("*.json")) if (r := self.get(p.stem)) and r.get("state") == "injecting"]


class TmuxSession:
    """The one existing native session, found and never created. ``known`` asks the harness whether its
    transcript exists on this machine; ``target`` finds its live tmux window by the session id in the window
    metadata; ``paste`` loads the text into a tmux buffer and pastes it."""

    def __init__(self, tmux: Tmux, tmux_session: str, meta_dir: Path, session_id: str, known):
        self.tmux, self.tmux_session = tmux, tmux_session
        self.meta_dir, self.session_id, self._known = Path(meta_dir), session_id, known

    def known(self) -> bool:
        try:
            return bool(self._known(self.session_id))
        except OSError:
            return False

    def target(self) -> str | None:
        for path in sorted(self.meta_dir.glob("*.json")):
            if path.name.endswith((".progress.json", ".settings.json", ".events.jsonl")):
                continue
            try:
                meta = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict) or meta.get("session_id") != self.session_id:
                continue
            if self.tmux.windows(self.tmux_session).get(path.stem):     # window name = metadata key, alive
                return f"={self.tmux_session}:={path.stem}"
        return None

    def paste(self, target: str, text: str) -> bool:
        buf = f"mr-meta-{uuid.uuid4().hex[:8]}"
        file = self.meta_dir / f"{buf}.prompt"
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        try:
            if self.tmux.run("load-buffer", "-b", buf, str(file)).returncode != 0:
                return False
            if self.tmux.run("paste-buffer", "-p", "-d", "-b", buf, "-t", target).returncode != 0:
                return False
            return self.tmux.run("send-keys", "-t", target, "Enter").returncode == 0
        finally:
            file.unlink(missing_ok=True)


class Settings:
    """``[session_metadata]``: off unless ``enabled = true``; then every identity field, the admission revision
    and a credential are required (absent admission fails closed at load, never at delivery time)."""
    KEYS = ("enabled", "agent", "credential_file", "credential_env", "interval_seconds", "allowed_sender_agent_ids",
            *IDENTITY_FIELDS)

    def __init__(self, section: dict, read_secret):
        unknown = set(section) - set(self.KEYS)
        if unknown:
            raise ValueError(f"[session_metadata] unknown keys: {', '.join(sorted(unknown))}")
        self.agent = str(section.get("agent") or "")
        self.identity = Identity(section.get("allowed_sender_agent_ids"),
                                 **{k: str(section.get(k) or "") for k in IDENTITY_FIELDS})
        if not self.agent:
            raise ValueError("[session_metadata] agent is required")
        self.credential = read_secret(section.get("credential_file"), section.get("credential_env"),
                                      "session_metadata credential")
        self.interval = float(section.get("interval_seconds", 5.0))
        if not 1.0 <= self.interval <= 300.0:
            raise ValueError("[session_metadata] interval_seconds must be 1..300")


def parse_settings(section, read_secret) -> "Settings | None":
    if not section or section.get("enabled", False) is not True:
        return None
    return Settings(section, read_secret)


class MetaClient:
    """Receiver side of the frozen routes (Mesh doc v3): ``X-Agent-Key`` is the agent's own credential,
    ``X-Runner-Token`` the token of this machine's registered chat runner (it names the runner_id)."""

    def __init__(self, base_url: str, credential: str, runner_token: str, timeout: int = 30, opener=None):
        loopback = re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base_url)
        if not (base_url.startswith("https://") or loopback):
            raise ValueError("the runner credential is sent only over https")
        if not runner_token:
            raise ValueError("a registered runner token is required")
        self._credential, self._token = credential, runner_token
        self.admission_revision: int | None = None          # server-assigned, learned from registration

        real = opener or urllib.request.urlopen

        def open_with_headers(req, timeout=None):
            req.add_header("X-Agent-Key", self._credential)
            req.add_header("X-Runner-Token", self._token)
            resp = real(req, timeout=timeout)
            self._last_status = getattr(resp, "status", None)         # the revoke must see the exact 204, not "no error"
            return resp
        self._mesh = MeshClient(base_url, "", timeout=timeout, opener=open_with_headers)
        self._mesh.base = base_url.rstrip("/") + "/api/v1/runner/existing-session"

    def __repr__(self) -> str:
        return f"MetaClient({self._mesh.base!r})"

    def register(self, identity: Identity) -> dict:
        """The answer is checked against the local identity: a mismatch fails closed (no receiver)."""
        got = self._mesh._request("POST", "/receivers", identity.registration()) or {}
        f = identity.fields
        if any(got.get(k) != f[k] for k in IDENTITY_FIELDS) or got.get("capability") != CAPABILITY:
            raise MeshError(0, "registration answer does not match the local identity")
        rev = got.get("admission_revision")
        if not isinstance(rev, int) or isinstance(rev, bool):
            raise MeshError(0, "registration answer has no admission_revision")
        if not is_uuid(got.get("receiver_id")):                    # an admission that cannot be named cannot be revoked
            raise MeshError(0, "registration answer has no valid receiver_id")
        self.admission_revision = rev
        self.receiver_id = got.get("receiver_id")
        return got

    def revoke(self, receiver_id: str) -> None:
        """Frozen ``DELETE /receivers/:receiver_id`` (204): revokes exactly that admission. Any other answer raises."""
        if not is_uuid(receiver_id):
            raise MeshError(0, "receiver_id is not a uuid")
        self._last_status = None
        self._mesh._request("DELETE", f"/receivers/{receiver_id}")
        if self._last_status != 204:
            raise MeshError(self._last_status or 0, "revoke was not answered with 204")

    def pending(self, identity: Identity) -> list[dict]:
        out = self._mesh._request("GET", "/deliveries", None, {"host_id": identity.fields["host_id"],
                                                              "session_id": identity.fields["session_id"]})
        return list((out or {}).get("deliveries") or [])

    def report(self, delivery_id: str, state: str, identity: Identity, error_code: str = "") -> dict:
        """Frozen ACK: one endpoint, body {host_id, session_id, admission_revision, state, error_code?}."""
        body = {"host_id": identity.fields["host_id"], "session_id": identity.fields["session_id"],
                "admission_revision": self.admission_revision, "state": state}
        if error_code:
            body["error_code"] = error_code
        return self._mesh._request("POST", f"/deliveries/{delivery_id}/ack", body)


class MetaReceiver:
    def __init__(self, identity: Identity, session, journal: Journal, client, ack_dir: Path):
        self.id, self.session, self.journal, self.client = identity, session, journal, client
        self.ack_dir = Path(ack_dir)
        self.ack_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ------------------------------------------------------------------ validation (no side effects)
    def validate(self, d: dict) -> None:
        if not isinstance(d, dict):
            raise Refused(ERR_SHAPE)
        if set(d) - REQUEST_FIELDS:
            raise Refused(ERR_FORBIDDEN)                     # command / argv / anything not in the contract
        if d.get("capability") != CAPABILITY:
            raise Refused(ERR_FORBIDDEN)
        text, rid = d.get("text"), d.get("request_id")
        if not isinstance(text, str) or not isinstance(rid, str) or not isinstance(d.get("target"), dict):
            raise Refused(ERR_SHAPE)
        if not is_uuid(rid) or not is_uuid(d.get("delivery_id")):
            raise Refused(ERR_SHAPE)
        try:
            size = len(text.encode())                        # an unpaired surrogate is not encodable: typed refusal
        except UnicodeEncodeError:
            raise Refused(ERR_SHAPE) from None
        if not text or size > MAX_TEXT_BYTES or d.get("text_sha256") != text_sha256(text):
            raise Refused(ERR_SHAPE)
        if d["target"] != self.id.fields:                    # exact match of all six fields, no subset, no extras
            raise Refused(ERR_TARGET)
        rev = self.client.admission_revision
        if rev is None or d.get("expected_admission_revision") != rev:        # unregistered = fail closed
            raise Refused(ERR_REVISION)

    def receipt(self, d: dict, rec: dict) -> dict:
        return {"delivery_id": rec["delivery_id"], "request_id": rec["request_id"], "text_sha256": rec["text_sha256"],
                "state": rec["state"], "receiver_identity": dict(self.id.fields),
                "received_at": rec.get("received_at"), "acknowledged_at": rec.get("acknowledged_at"),
                "error_code": rec.get("error_code")}

    # ------------------------------------------------------------------ one delivery
    def handle(self, d: dict) -> dict:
        rid = d.get("request_id") if isinstance(d, dict) else None
        did = d.get("delivery_id") if isinstance(d, dict) else None
        rid, did = (rid if is_uuid(rid) else None), (did if is_uuid(did) else "")   # untrusted: never a path
        try:
            self.validate(d)
        except Refused as e:
            return self._fail(d, e.code, rid, did)
        digest, prior = d["text_sha256"], self.journal.get(rid)
        if prior:
            if prior["text_sha256"] != digest or prior["delivery_id"] != did:
                return self._fail(d, ERR_CONFLICT, rid, did, persist=False)
            if prior["state"] == "injecting":                # crash after a possible paste: never paste again
                return self._report(d, self._settle(prior, uncertain=True))
            return self._report(d, prior)                    # same receipt again (a lost report is retried), zero new injection
        if not self.session.known():
            return self._fail(d, ERR_SESSION, rid, did)
        target = self.session.target()
        if target is None:
            return self._fail(d, ERR_SESSION, rid, did)
        rec = {"delivery_id": did, "request_id": rid, "text_sha256": digest, "state": "injecting",
               "bytes": len(d["text"].encode())}
        self.journal.put(rid, rec)                           # intent is durable BEFORE the first byte moves
        if not self.session.paste(target, self._framed(d)):
            rec.update(state="uncertain", error_code=ERR_PASTE)   # a refused paste may still be partial
            self.journal.put(rid, rec)
            return self._report(d, rec)
        rec.update(state="received", received_at=_now())
        self.journal.put(rid, rec)
        log.info("metadata delivery %s: received (%d bytes, sha %s)", did[:8], rec["bytes"], digest[:8])
        return self._report(d, rec)

    def _framed(self, d: dict) -> str:
        return (f"[readonly_metadata delivery_id={d['delivery_id']} request_id={d['request_id']}]\n"
                f"{d['text']}\n"
                f"[Acknowledge by writing {self.ack_dir / (d['delivery_id'] + '.json')} with delivery_id, "
                f"request_id, hostname, session_id]")

    def _settle(self, rec: dict, uncertain: bool) -> dict:
        ack = self.read_ack(rec)
        if ack:
            rec.update(state="acknowledged", acknowledged_at=_now())
        elif uncertain:
            rec.update(state="uncertain", error_code=ERR_UNCERTAIN)
        self.journal.put(rec["request_id"], rec)
        return rec

    def read_ack(self, rec: dict) -> bool:
        try:
            ack = json.loads((self.ack_dir / f"{rec['delivery_id']}.json").read_text())
        except (OSError, ValueError):
            return False
        return (isinstance(ack, dict) and ack.get("delivery_id") == rec["delivery_id"]
                and ack.get("request_id") == rec["request_id"]
                and ack.get("hostname") == self.id.fields["hostname"]
                and ack.get("session_id") == self.id.fields["session_id"])

    def reconcile(self) -> list[dict]:
        """Producer answers for received deliveries; leftover ``injecting`` records fail closed."""
        out = []
        for rec in self.journal.open_injections():
            out.append(self._report({}, self._settle(rec, uncertain=True)))
        for p in sorted(self.journal.dir.glob("*.json")):
            rec = self.journal.get(p.stem)
            if rec and rec.get("state") == "received" and self.read_ack(rec):
                rec.update(state="acknowledged", acknowledged_at=_now())
                self.journal.put(rec["request_id"], rec)
                out.append(self._report({}, rec))
        return out

    def _fail(self, d, code: str, rid, did: str, persist: bool = True) -> dict:
        digest = d.get("text_sha256") if isinstance(d, dict) else ""
        rec = {"delivery_id": did, "request_id": rid, "text_sha256": digest if _is_sha256(digest) else "",
               "state": "failed", "error_code": code}
        if persist and rid and did and self.journal.get(rid) is None:
            self.journal.put(rid, rec)
        log.info("metadata delivery %s: refused %s", did[:8], code)
        return self._report(d, rec)

    def _report(self, d, rec: dict) -> dict:
        receipt = self.receipt(d, rec)
        state = {"uncertain": "failed"}.get(rec["state"], rec["state"])
        if rec["state"] == "uncertain":
            receipt["error_code"] = rec.get("error_code") or ERR_UNCERTAIN
        if rec.get("delivery_id"):
            try:
                if state == "acknowledged":                  # a lost "received" report must not make this a 409
                    self.client.report(rec["delivery_id"], "received", self.id)
                self.client.report(rec["delivery_id"], state, self.id, receipt.get("error_code") or "")
            except (MeshError, OSError) as e:
                log.warning("metadata delivery %s: report %s failed: %s", rec["delivery_id"][:8], state,
                            type(e).__name__)
        return receipt

    def step(self) -> int:
        """Claim what is queued for exactly this receiver and handle it; then reconcile."""
        done = 0
        for d in self.client.pending(self.id):
            self.handle(d)
            done += 1
        self.reconcile()
        return done


def _is_sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def receiver_loop(receiver: MetaReceiver, stop, interval: float) -> None:
    """Register this one receiver, then poll its delivery route until ``stop``. Until registration succeeds
    there is no admission revision: nothing is polled and nothing is delivered (fail closed), and the
    registration is retried on every tick."""
    registered = False
    while not stop.is_set():
        try:
            if not registered:
                receiver.client.register(receiver.id)
                registered = True
            receiver.step()
        except (MeshError, OSError) as e:
            log.warning("metadata receiver: %s", type(e).__name__)
        except Exception as e:
            log.warning("metadata receiver loop: %s", type(e).__name__)
        if stop.wait(interval):
            return

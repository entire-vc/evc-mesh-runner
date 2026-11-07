"""``host_local_qa_assignment_v1``: a separate, default-OFF capability of the existing-session receiver.

Frozen contract: ``docs/host-local-qa-assignment.md``. The frozen bytes (brief, policy, schema) are PRIVATE: they are not in
this package, the operator opts in with ``bundle_dir`` and they are verified against the sha256 pins below. One fixed
brief text is delivered to ONE already admitted native producer in its local iTerm session. It is not a command channel:
the sender envelope is exactly ``schema.json`` (no extra property at any depth), the text is the pinned ``brief.txt``, and
nothing in it is executed by anything here.

It reuses, and never duplicates, the existing machinery: the same ``Journal`` / ``ConsumerLock`` / ledger pin, the same
``ITermSession`` injector. What is new:

* its own scoped credential, its own server-side admission (revision), its own routes under ``/runner/host-local-qa/v1``;
* an authoritative backend ``/check`` asked immediately before the paste AND again immediately before the Enter (the iTerm
  paste is split into stage / submit for exactly that), after the journal intent; a timeout, a malformed or mismatching
  answer, a revoke or an expiry is a refusal and a cached success never authorises anything;
* the SAME ``/check`` as a host-local guard (``guard``/``session-receiver qa-check``) that the operation owner calls before
  each normal mutation. It executes nothing and changes nothing.

Never: SSH/CDP, global keystrokes, a new grant, creating/adopting/restarting a producer, touching the legacy admission, the
ledger or the producer on disable/revoke. A transport/producer ACK is not an install receipt.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import coalesced_http
from . import iterm_transport as it
from . import meta_delivery as md

log = logging.getLogger("mesh_runner.qa_assignment")

CAPABILITY = "host_local_qa_assignment_v1"
SECTION = "host_local_qa_assignment_v1"
KEYS = ("enabled", "bundle_dir", "credential_file", "credential_env", "allowed_sender_agent_ids", "expires", "interval_seconds",
        "target", "producer")
PUBLIC = Path(__file__).with_name("qa_assignment_v1")    # only the public conformance ids ship
DATA: Path | None = None            # the PRIVATE frozen bundle: never packaged, set only by an explicit local opt-in (bundle_dir)
PINS = {"brief.txt": "5422e262a6833b506e33203ed8212cc828e2393993ecd45da91ced5d7df0a96f",
        "policy.json": "3d3f06d516c5b4268e01342cb79c58f1be90ba1775de20e1ff5dc13e69f8e9cc",
        "schema.json": "eb3dc20f9d25ebd95bbd8f831ccd5c334d4bafd6b7b3e52c81942b05b48d9850"}
CHECK_BASE = "https://mesh.entire.host/api/v1"          # fixed: never payload data, never configuration
ROUTE = "/runner/host-local-qa/v1"
MAX_EXPIRY_SECONDS = 28800
CLOCK_SKEW_SECONDS = 120                                 # a /check answer older or newer than this is not "now"
MAX_RESPONSE_BYTES = 1 << 20
TIMEOUT = 15
ADMISSION_FILE = "admission-qa-host-local-v1.json"       # namespaced under the SAME ledger root as the legacy admission
REVOKED_FILE = "admission-qa-host-local-v1-revoked.json"
ADMISSION_LOCK = "admission-qa-host-local-v1.lock"
ACK_DIR = "acks-qa-host-local-v1"
BOUNDARIES = ("qa_stop", "qa_start", "asset_replace_main_js", "asset_replace_manifest_json",
              "asset_replace_styles_css", "channel_start", "controller_lifecycle")
TARGET_FIELDS = md.IDENTITY_FIELDS
PRODUCER_FIELDS = ("transport", "terminal_session_id", "tty", "producer_pid", "producer_started")
CHECK_FIELDS = ("receiver_id", "admission_revision", "delivery_id", "request_id", "capability", "policy_sha256",
                "text_sha256", "target", "producer", "expires")
ENVELOPE_EXTRA = {"delivery_id", "created_at"}           # the receiver adds exactly these to the sender envelope
ERR_EXPIRED, ERR_RECEIVER, ERR_PRODUCER = "expired", "wrong_receiver", "wrong_producer"
ERR_REVOKED, ERR_CHECK_DOWN, ERR_CHECK_BAD, ERR_CHECK_DENIED = ("revoked", "check_unavailable", "check_malformed",
                                                                "check_denied")
ERR_CHECK_MISMATCH = "check_mismatch"


class QARefusal(Exception):
    """A fail-closed condition with a stable, secret-free ``code``."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


# ---------------------------------------------------------------------- pinned bytes and the schema
_loaded: dict | None = None


def configure_bundle(path) -> None:
    """Point the loader at the operator's local copy of the frozen bundle (the ``bundle_dir`` opt-in). Nothing is read here."""
    global DATA, _loaded
    DATA, _loaded = Path(path), None


def pinned() -> dict:
    """brief text, policy and schema, read from the operator's local frozen bundle (``bundle_dir``; the public package carries
    none of these bytes) and compared with the pins in this file. Not configured, a missing file or any difference is a
    refusal raised before any state is touched (a changed frozen byte needs a new pin and an affected-diff review)."""
    global _loaded
    if _loaded is not None:
        return _loaded
    if DATA is None:
        raise QARefusal("pinned_bundle_not_configured")
    raw = {}
    for name, want in PINS.items():
        try:
            data = (DATA / name).read_bytes()
        except OSError:
            raise QARefusal("pinned_bytes_missing", name) from None
        if hashlib.sha256(data).hexdigest() != want:
            raise QARefusal("pinned_bytes_changed", name)
        raw[name] = data
    try:
        _loaded = {"text": raw["brief.txt"].decode("utf-8"), "policy": json.loads(raw["policy.json"]),
                   "schema": json.loads(raw["schema.json"]), "policy_sha256": PINS["policy.json"],
                   "text_sha256": PINS["brief.txt"]}
    except ValueError:
        raise QARefusal("pinned_bytes_changed", "parse") from None
    return _loaded


def check_schema(value, schema: dict) -> None:
    """The subset of JSON Schema that ``schema.json`` uses, strict: unknown properties are ``forbidden_field`` at every
    depth, ``bool`` is never an integer, and a string pattern must match the WHOLE string."""
    if "const" in schema:
        const = schema["const"]
        if type(value) is not type(const) or value != const:
            raise QARefusal("bad_request")
        return
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise QARefusal("bad_request")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise QARefusal("forbidden_field")
        if set(schema.get("required", [])) - set(value):
            raise QARefusal("bad_request")
        for key, sub in props.items():
            if key in value:
                check_schema(value[key], sub)
    elif kind == "integer":
        if type(value) is not int or value < schema.get("minimum", -(1 << 62)):
            raise QARefusal("bad_request")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1 << 20):
            raise QARefusal("bad_request")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise QARefusal("bad_request")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise QARefusal("bad_request") from None
    else:
        raise QARefusal("bad_request")


def now_epoch() -> int:
    return int(time.time())


def utc(epoch: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        pass
    try:                                                     # a backend may send fractional seconds or an offset
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- the opt-in settings
class QAIdentity:
    """Target + producer of THIS receiver, exactly as configured (and equal to the metadata identity/iTerm pin)."""

    def __init__(self, target: dict, producer: dict, allowed_senders: list, expires: int):
        self.target, self.producer = dict(target), dict(producer)
        self.allowed_senders, self.expires = list(allowed_senders), expires
        self.fields = self.target                              # the six fields: same name MetaReceiver users know

    def registration(self) -> dict:
        """Frozen body of ``POST /runner/host-local-qa/v1/receivers``; the runner id travels in X-Runner-Token."""
        return {"schema_version": 1, "capability": CAPABILITY, "policy_sha256": pinned()["policy_sha256"],
                "target": dict(self.target), "producer": dict(self.producer), "expires": self.expires,
                "allowed_sender_agent_ids": list(self.allowed_senders)}

    def binding_sha256(self) -> str:
        # the policy PIN, not the bundle bytes: identifying a recorded admission (and so revoking it) needs no bundle
        return digest({"capability": CAPABILITY, "policy_sha256": PINS["policy.json"], "target": self.target,
                       "producer": self.producer})


@dataclass
class QASettings:
    identity: QAIdentity
    credential: str
    interval: float


def parse_section(section, read_secret, metadata_identity: md.Identity, metadata_credential: str, pin,
                  transport: str, allow_expired: bool = False) -> "QASettings | None":
    """``[host_local_qa_assignment_v1]``. Absent or ``enabled`` not true: None (OFF; default). Unknown keys are refused even
    when off. When on: the target and producer are restated here and must equal the metadata identity and the iTerm pin of the
    SAME ledger; the credential must be a different one from the metadata credential. ``allow_expired`` is for the disable
    path only (``qa-revoke``, ``revoke``, ``status``): a lapsed expiry must never make the admission undeletable; the
    receiver, the guard and ``qa-observe`` load strictly and refuse an expired section."""
    if section is None:
        return None
    if not isinstance(section, dict):                    # a falsy non-table ([] / "" / 0 / false) is malformed, not OFF
        raise QARefusal("config_invalid", "section")
    unknown = set(section) - set(KEYS)
    if unknown:
        raise QARefusal("config_unknown_keys", ", ".join(sorted(unknown)))
    if section.get("enabled", False) is not True:
        return None
    if transport != "iterm":
        raise QARefusal("qa_needs_iterm_transport")
    bundle = section.get("bundle_dir")
    if not isinstance(bundle, str) or not bundle or not os.path.isabs(bundle):
        raise QARefusal("qa_bundle_dir_required")
    configure_bundle(bundle)
    try:
        p = pinned()                                     # verified before the receiver mutates anything
    except QARefusal:
        if not allow_expired:
            raise
        p = None                                         # the disable path must work without the bundle: it only deletes
    target, producer = section.get("target"), section.get("producer")
    if p is not None:
        try:
            check_schema(target, p["schema"]["properties"]["target"])
            check_schema(producer, p["schema"]["properties"]["producer"])
        except QARefusal:
            raise QARefusal("config_invalid", "target_or_producer") from None
    elif (not isinstance(target, dict) or not isinstance(producer, dict) or set(target) != set(TARGET_FIELDS)
          or set(producer) != set(PRODUCER_FIELDS)):           # no schema without the bundle: exact keys, never a KeyError
        raise QARefusal("config_invalid", "target_or_producer")
    senders = section.get("allowed_sender_agent_ids")
    if not isinstance(senders, list) or not senders or not all(isinstance(x, str) for x in senders):
        raise QARefusal("config_invalid", "allowed_sender_agent_ids")        # also without the bundle: never a TypeError
    if not all(isinstance(v, str) for v in target.values()) or not (
            all(isinstance(producer[k], str) for k in ("transport", "terminal_session_id", "tty", "producer_started"))
            and type(producer["producer_pid"]) is int):
        raise QARefusal("config_invalid", "target_or_producer")
    if target != metadata_identity.fields:
        raise QARefusal("qa_target_differs_from_identity")
    started = " ".join(str(producer["producer_started"]).split())
    if (producer["terminal_session_id"] != pin.iterm_session_id or producer["tty"] != pin.tty
            or producer["producer_pid"] != pin.producer_pid or started != " ".join(pin.producer_started.split())
            or producer["producer_started"] != started):
        raise QARefusal("qa_producer_differs_from_pin")
    if p is not None and senders != p["policy"]["allowed_sender_agent_ids"]:
        raise QARefusal("qa_senders_differ_from_policy")
    expires = section.get("expires")
    if type(expires) is not int or (expires <= now_epoch() and not allow_expired) or expires - now_epoch() > MAX_EXPIRY_SECONDS:
        raise QARefusal("qa_expiry_invalid")
    try:
        credential = read_secret(section.get("credential_file"), section.get("credential_env"), "qa credential")
    except Exception as e:
        raise QARefusal("qa_credential_unreadable", type(e).__name__) from None
    if credential == metadata_credential:
        raise QARefusal("qa_credential_not_separate")
    interval = section.get("interval_seconds", 5.0)
    if type(interval) not in (int, float) or not 1.0 <= interval <= 300.0:
        raise QARefusal("config_invalid", "interval_seconds")
    return QASettings(QAIdentity(target, producer, senders, expires), credential, float(interval))


# ---------------------------------------------------------------------- the admission record (its own file, same root)
def _write_private(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(obj))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


class _AdmissionLock:
    """Short flock around the QA admission file only (register vs revoke); the ConsumerLock is a different thing."""

    def __init__(self, root: Path):
        self.path = Path(root) / ADMISSION_LOCK

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        os.close(self.fd)


def read_admissions(root: Path, ident: QAIdentity, missing_ok: bool = False) -> list[dict]:
    try:
        rec = json.loads((Path(root) / ADMISSION_FILE).read_text())
    except FileNotFoundError:
        if missing_ok:
            return []
        raise QARefusal("admission_unknown") from None
    except (OSError, ValueError):
        raise QARefusal("admission_unknown") from None
    if (not isinstance(rec, dict) or rec.get("capability") != CAPABILITY or rec.get("binding_sha256") != ident.binding_sha256()
            or rec.get("runner_id") != ident.target["runner_id"]):
        raise QARefusal("admission_identity_mismatch")
    items = rec.get("receivers")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) and md.is_uuid(i.get("receiver_id"))
                                                           for i in items):
        raise QARefusal("admission_unknown")
    return items


def write_admissions(root: Path, ident: QAIdentity, items: list[dict]) -> None:
    _write_private(Path(root) / ADMISSION_FILE, {"capability": CAPABILITY, "binding_sha256": ident.binding_sha256(),
                                                 "runner_id": ident.target["runner_id"], "receivers": items})


# ---------------------------------------------------------------------- HTTP client
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None                                            # a 3xx is an HTTPError: no redirect, no fallback origin


def default_open(req, timeout=TIMEOUT):
    opener = coalesced_http.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)   # no environment proxy
    return opener.open(req, timeout=timeout)


class QAClient:
    """Receiver side of the QA routes and the host-local check. The scoped credential travels ONLY in ``Authorization``;
    the registered runner's token in ``X-Runner-Token``; no body ever carries a text or a token."""

    def __init__(self, base_url: str, credential: str, runner_token: str, ident: QAIdentity, ledger_root: Path,
                 opener=None, clock=now_epoch, stop=None):
        loopback = re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base_url)
        if not (base_url.startswith("https://") or loopback):
            raise ValueError("the QA credential is sent only over https")
        if not runner_token or not credential:
            raise ValueError("a QA credential and a registered runner token are required")
        self._credential, self._token = credential, runner_token
        self.base = base_url.rstrip("/") + "/api/v1" + ROUTE
        self.check_base = CHECK_BASE + ROUTE
        self.ident, self.root, self.clock, self._open, self.stop = ident, Path(ledger_root), clock, opener or default_open, stop
        self.admission_revision: int | None = None
        self.receiver_id: str | None = None
        self.record_failed = False

    def __repr__(self) -> str:
        return f"QAClient({self.base!r})"

    # -- transport
    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._credential)
        req.add_header("X-Runner-Token", self._token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            resp = self._open(req, timeout=TIMEOUT)
            status = getattr(resp, "status", 200)
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise md.MeshError(e.code, "qa route refused") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise md.MeshError(0, "qa route unreachable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QARefusal(ERR_CHECK_BAD)
        if not raw.strip():
            return status, {}
        try:
            out = json.loads(raw)
        except ValueError:
            raise QARefusal(ERR_CHECK_BAD) from None
        return status, out if isinstance(out, dict) else {}

    # -- receiver routes
    def register(self, ident: QAIdentity) -> dict:
        got = self._call("POST", self.base + "/receivers", ident.registration())[1]
        t = ident.target
        for k, want in (("capability", CAPABILITY), ("policy_sha256", pinned()["policy_sha256"]), ("target", t),
                        ("producer", ident.producer), ("workspace_id", t["workspace_id"]), ("agent_id", t["agent_id"]),
                        ("runner_id", t["runner_id"])):
            if k in got and got[k] != want:
                raise md.MeshError(0, "registration answer does not match the local identity")
        if any(k not in got for k in ("workspace_id", "agent_id", "runner_id")):
            raise md.MeshError(0, "registration answer lacks the authenticated ids")
        rev, rid = got.get("admission_revision"), got.get("receiver_id")
        if type(rev) is not int or rev < 1:
            raise md.MeshError(0, "registration answer has no admission_revision")
        if not md.is_uuid(rid):
            raise md.MeshError(0, "registration answer has no valid receiver_id")
        try:
            with _AdmissionLock(self.root):
                items = [i for i in read_admissions(self.root, ident, True) if i["receiver_id"] != rid]
                write_admissions(self.root, ident, items + [{"receiver_id": rid, "admission_revision": rev,
                                                             "expires": ident.expires}])
        except (OSError, QARefusal) as e:
            # an admission nobody can name later is one nobody can revoke: undo it, serve nothing, stop re-registering
            self.record_failed, self.admission_revision = True, None
            try:
                self.revoke(rid)
                log.error("qa receiver: admission record not writable (%s); the fresh admission was revoked", type(e).__name__)
            except (md.MeshError, OSError, ValueError):
                log.error("qa receiver: admission record not writable AND admission %s was NOT revoked; revoke it by hand",
                          str(rid)[:8])
            if self.stop is not None:
                self.stop.set()
            raise md.MeshError(0, "admission record not written") from None
        self.admission_revision, self.receiver_id = rev, rid
        return got

    def revoke(self, receiver_id: str) -> None:
        """``DELETE /receivers/:id`` answered with exactly 204; anything else raises."""
        if not md.is_uuid(receiver_id):
            raise md.MeshError(0, "receiver_id is not a uuid")
        status, _ = self._call("DELETE", f"{self.base}/receivers/{receiver_id}")
        if status != 204:
            raise md.MeshError(status or 0, "revoke was not answered with 204")

    def pending(self, ident: QAIdentity) -> list[dict]:
        q = urllib.parse.urlencode({"host_id": ident.target["host_id"], "session_id": ident.target["session_id"]})
        out = self._call("GET", f"{self.base}/deliveries?{q}")[1]
        items = out.get("deliveries") or []
        return list(items) if isinstance(items, list) else []

    def report(self, delivery_id: str, state: str, body: dict) -> dict:
        return self._call("POST", f"{self.base}/deliveries/{delivery_id}/ack", dict(body, state=state))[1]

    # -- the authoritative check (receiver and host-local guard use this one function)
    def check(self, request: dict) -> dict:
        """Ask the backend whether this exact delivery is still authorised, now. Returns the verified answer or raises
        ``QARefusal``; never returns a partially verified one. No answer is cached."""
        if set(request) != set(CHECK_FIELDS):
            raise QARefusal(ERR_CHECK_BAD, "request")
        try:
            _, got = self._call("POST", f"{self.check_base}/deliveries/{request['delivery_id']}/check", request)
        except md.MeshError as e:
            if e.status in (401, 403, 404, 409, 410):
                raise QARefusal(ERR_CHECK_DENIED, str(e.status)) from None
            raise QARefusal(ERR_CHECK_DOWN, str(e.status)) from None
        if got.get("active") is not True:
            raise QARefusal(ERR_REVOKED if got.get("active") is False else ERR_CHECK_BAD)
        if set(got) != set(CHECK_FIELDS) | {"active", "checked_at"}:
            raise QARefusal(ERR_CHECK_BAD, "fields")
        for k in CHECK_FIELDS:
            if got[k] != request[k] or type(got[k]) is not type(request[k]):
                raise QARefusal(ERR_CHECK_MISMATCH, k)
        at = parse_utc(got["checked_at"])
        if at is None or abs(at - self.clock()) > CLOCK_SKEW_SECONDS:
            raise QARefusal(ERR_CHECK_BAD, "checked_at")
        if self.clock() >= request["expires"]:
            raise QARefusal(ERR_EXPIRED)
        return got


# ---------------------------------------------------------------------- receiver
class QAReceiver(md.MetaReceiver):
    """MetaReceiver for the QA capability: the same journal, session (injector) and ack discipline, a stricter envelope, and
    the authority asked at every boundary."""

    def __init__(self, ident: QAIdentity, session, journal: md.Journal, client: QAClient, root: Path, clock=now_epoch,
                 observe=None):
        super().__init__(ident, session, journal, client, Path(root) / ACK_DIR)
        self.ident, self.root, self.clock, self.observe = ident, Path(root), clock, observe
        self._reason = ""

    # ------------------------------------------------------------------ validation (no side effects)
    def validate(self, d: dict) -> None:
        if not isinstance(d, dict):
            raise md.Refused(md.ERR_SHAPE)
        if set(d) - set(pinned()["schema"]["properties"]) - ENVELOPE_EXTRA:
            raise md.Refused(md.ERR_FORBIDDEN)
        envelope = {k: v for k, v in d.items() if k not in ENVELOPE_EXTRA}
        try:
            check_schema(envelope, pinned()["schema"])
        except QARefusal as e:
            raise md.Refused(e.code) from None
        created = d.get("created_at")
        if not md.is_uuid(d.get("delivery_id")) or not isinstance(created, str) or not 0 < len(created) <= 64:
            raise md.Refused(md.ERR_SHAPE)
        if d["target"] != self.ident.target:                  # all six fields, indistinguishable whichever one differs
            raise md.Refused(md.ERR_TARGET)
        if d["producer"] != self.ident.producer:
            raise md.Refused(ERR_PRODUCER)
        rev = self.client.admission_revision
        if rev is None or d["expected_admission_revision"] != rev:
            raise md.Refused(md.ERR_REVISION)
        if d["receiver_id"] != self.client.receiver_id:
            raise md.Refused(ERR_RECEIVER)

    @staticmethod
    def envelope_of(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ENVELOPE_EXTRA}

    def check_request(self, rec: dict) -> dict:
        q = rec["qa"]
        return {"receiver_id": q["receiver_id"], "admission_revision": q["admission_revision"],
                "delivery_id": rec["delivery_id"], "request_id": rec["request_id"], "capability": CAPABILITY,
                "policy_sha256": q["policy_sha256"], "text_sha256": rec["text_sha256"], "target": q["target"],
                "producer": q["producer"], "expires": q["expires"]}

    def receipt(self, d: dict, rec: dict) -> dict:
        q = rec.get("qa") or {}
        return {"delivery_id": rec.get("delivery_id"), "request_id": rec.get("request_id"), "schema_version": 1,
                "capability": CAPABILITY, "target": q.get("target"), "producer": q.get("producer"),
                "receiver_id": q.get("receiver_id"), "admission_revision": q.get("admission_revision"),
                "policy_sha256": q.get("policy_sha256"), "text_sha256": rec.get("text_sha256"),
                "expires": q.get("expires"), "state": rec.get("state"), "received_at": rec.get("received_at"),
                "acknowledged_at": rec.get("acknowledged_at"), "result_comment_url": None,
                "error_code": rec.get("error_code")}

    # ------------------------------------------------------------------ the authority
    def _local_active(self) -> str:
        """The recorded admission is still this receiver's own and has not been revoked locally."""
        try:
            ids = [i["receiver_id"] for i in read_admissions(self.root, self.ident)]
        except QARefusal:
            return ERR_REVOKED
        return "" if self.client.receiver_id in ids else ERR_REVOKED

    def authority(self, rec: dict) -> str:
        """"" or the typed refusal. Called right after the journal intent, again before the Enter; asks the backend each time."""
        now = self.clock()
        if now >= rec["qa"]["expires"]:
            return ERR_EXPIRED
        if now >= self.ident.expires:
            return ERR_EXPIRED
        local = self._local_active()
        if local:
            return local
        try:
            before = self.observe() if self.observe else None      # the actual terminal<->native<->PID relation, now
            self.client.check(self.check_request(rec))
            if before is not None and not _same_binding(before, self.observe()):
                return "binding_changed_during_check"
            if before is not None and (before["target"] != rec["qa"]["target"] or before["producer"] != rec["qa"]["producer"]):
                return "observed_binding_mismatch"
        except QARefusal as e:
            return e.code
        return ""

    # ------------------------------------------------------------------ one delivery
    def _handle(self, d: dict) -> dict:
        rid = d.get("request_id") if isinstance(d, dict) else None
        did = d.get("delivery_id") if isinstance(d, dict) else None
        rid, did = (rid if md.is_uuid(rid) else None), (did if md.is_uuid(did) else "")
        prior = self.journal.get(rid) if rid else None
        if prior:
            # an existing request is immutable: ANY change (even one that would not validate) is a conflict, decided BEFORE
            # validation so a changed producer/capability/unknown field is 409 and never a validation error
            same = (prior.get("capability") == CAPABILITY and self._well_formed(prior) and prior["delivery_id"] == did
                    and prior.get("envelope_sha256") == digest(self.envelope_of(d))
                    and prior.get("created_at") == (d.get("created_at") if isinstance(d, dict) else None))
            if not same:
                return self._conflict(d, rid, did, prior)
        try:
            self.validate(d)
        except md.Refused as e:
            return self._fail(d, e.code, rid, did)
        env_sha = digest(self.envelope_of(d))
        if prior:
            if prior["state"] == "injecting":
                return self._report(d, self._settle(prior, uncertain=True))
            return self._report(d, prior)                                            # same receipt, zero new injection
        qa = {"receiver_id": d["receiver_id"], "admission_revision": d["expected_admission_revision"],
              "policy_sha256": d["policy_sha256"], "target": d["target"], "producer": d["producer"], "expires": d["expires"]}
        rec = {"capability": CAPABILITY, "delivery_id": did, "request_id": rid, "text_sha256": d["text_sha256"],
               "envelope_sha256": env_sha, "created_at": d["created_at"], "state": "injecting", "bytes": len(d["text"].encode()), "qa": qa}
        if self.clock() >= d["expires"] or d["expires"] - self.clock() > MAX_EXPIRY_SECONDS:
            return self._fail(d, ERR_EXPIRED, rid, did)
        if not self.session.known():
            return self._fail(d, md.ERR_SESSION, rid, did)
        target = self.session.target()
        if target is None:
            return self._fail(d, md.ERR_SESSION, rid, did)
        self.journal.put(rid, rec)                           # intent is durable BEFORE the first byte can move
        self._reason = ""

        def guard() -> str:
            self._reason = self.authority(rec)
            return self._reason
        result = self.session.paste_guarded(target, self._framed(d), guard)
        if result == "refused":
            rec.update(state="failed", error_code=self._reason or md.ERR_PASTE)      # nothing was written
        elif result == "uncertain":
            rec.update(state="uncertain", error_code=self._reason or md.ERR_PASTE)   # text may sit unsubmitted: never replayed
        else:
            rec.update(state="received", received_at=md._now())
        self.journal.put(rid, rec)
        log.info("qa delivery %s: %s", did[:8], rec["state"])
        return self._report(d, rec)

    def _framed(self, d: dict) -> str:
        return (f"[{CAPABILITY} delivery_id={d['delivery_id']} request_id={d['request_id']} text_sha256={d['text_sha256']}]\n"
                f"{d['text']}\n"
                f"[Acknowledge by writing {self.ack_dir / (d['delivery_id'] + '.json')}: see docs/host-local-qa-assignment.md]")

    # ------------------------------------------------------------------ producer ACK, reconcile, report
    def read_ack(self, rec: dict) -> bool:
        """The producer's ACK is valid only when it names every identity and pin of this delivery."""
        try:
            ack = json.loads((self.ack_dir / f"{rec['delivery_id']}.json").read_text())
        except (OSError, ValueError):
            return False
        q = rec.get("qa") or {}
        want = {"delivery_id": rec["delivery_id"], "request_id": rec["request_id"], "receiver_id": q.get("receiver_id"),
                "admission_revision": q.get("admission_revision"), "capability": CAPABILITY,
                "policy_sha256": q.get("policy_sha256"), "text_sha256": rec["text_sha256"], "target": q.get("target"),
                "producer": q.get("producer")}
        return (isinstance(ack, dict) and set(ack) == set(want) | {"observed_at"}
                and all(ack[k] == v and type(ack[k]) is type(v) for k, v in want.items())
                and parse_utc(ack["observed_at"]) is not None)

    def _mine(self, rec) -> bool:
        return bool(rec) and rec.get("capability") == CAPABILITY

    @staticmethod
    def _well_formed(rec) -> bool:
        """The fields the receiver indexes are present and typed. A damaged record is never replayed, never settled and never
        allowed to crash the serve loop: it is skipped by reconcile and answered as a conflict on a replay."""
        return (isinstance(rec, dict) and isinstance(rec.get("state"), str) and md.is_uuid(rec.get("delivery_id"))
                and md.is_uuid(rec.get("request_id")) and isinstance(rec.get("text_sha256"), str))

    def reconcile(self) -> list[dict]:
        out = []
        with self.journal.lock:
            for p in sorted(self.journal.dir.glob("*.json")):
                rec = self.journal.get(p.stem)
                if not self._mine(rec):
                    continue
                if not self._well_formed(rec):
                    log.error("qa journal record %s is malformed: skipped, never replayed", p.stem[:8])
                    continue
                if rec["state"] == "injecting":                                       # leftover after a crash: fail closed
                    out.append(self._report({}, self._settle(rec, uncertain=True)))
                elif rec["state"] == "received" and self.read_ack(rec):
                    rec.update(state="acknowledged", acknowledged_at=md._now())
                    self.journal.put(rec["request_id"], rec)
                    out.append(self._report({}, rec))
        return out

    def _conflict(self, d, rid, did: str, prior: dict) -> dict:
        """A changed envelope for an existing request: the original is never touched. A DIFFERENT delivery of that request is
        reported failed/request_conflict to the backend under the original's own binding; a changed re-send of the SAME
        delivery is answered locally only, so the original delivery's state at the backend cannot be overwritten."""
        receipt = self._fail(d, md.ERR_CONFLICT, rid, did, persist=False)
        if not did or did == prior.get("delivery_id") or not isinstance(prior.get("qa"), dict):
            return receipt
        sha = d.get("text_sha256") if isinstance(d, dict) else None
        rec = {"capability": CAPABILITY, "delivery_id": did, "request_id": rid, "state": "failed",
               "error_code": md.ERR_CONFLICT, "qa": prior["qa"],
               "text_sha256": sha if md._is_sha256(sha) else prior["text_sha256"]}
        return self._report(d, rec)

    def _fail(self, d, code: str, rid, did: str, persist: bool = True) -> dict:
        digest_ = d.get("text_sha256") if isinstance(d, dict) else ""
        rec = {"capability": CAPABILITY, "delivery_id": did, "request_id": rid,
               "text_sha256": digest_ if md._is_sha256(digest_) else "", "state": "failed", "error_code": code}
        if persist and rid and did and self.journal.get(rid) is None:
            rec["envelope_sha256"] = digest(self.envelope_of(d)) if isinstance(d, dict) else ""
            rec["created_at"] = d.get("created_at") if isinstance(d, dict) else None
            self.journal.put(rid, rec)
        log.info("qa delivery %s: refused %s", did[:8], code)
        return self._report(d, rec)

    def _report(self, d, rec: dict) -> dict:
        receipt = self.receipt(d, rec)
        if not rec.get("delivery_id") or not rec.get("qa"):
            return receipt                                   # a refusal before any binding exists has nothing to report to
        state = rec["state"]
        body = {k: receipt[k] for k in CHECK_FIELDS if k not in ("delivery_id", "request_id")}
        body.update(delivery_id=rec["delivery_id"], request_id=rec["request_id"], capability=CAPABILITY,
                    observed_at=utc(self.clock()))
        if rec.get("error_code"):
            body["error_code"] = rec["error_code"]
        try:
            if state == "acknowledged":                      # queued -> acknowledged is forbidden: report received first
                self.client.report(rec["delivery_id"], "received", {k: v for k, v in body.items() if k != "error_code"})
            self.client.report(rec["delivery_id"], state, body)
        except (md.MeshError, QARefusal, OSError) as e:
            log.warning("qa delivery %s: report %s failed: %s", rec["delivery_id"][:8], state, type(e).__name__)
        return receipt

    def step(self) -> int:
        done = 0
        for d in self.client.pending(self.ident):
            self.handle(d)
            done += 1
        self.reconcile()
        return done


# ---------------------------------------------------------------------- actual local binding (read-only proof)
NATIVE_FLAGS = ("--resume", "-r", "--session-id", "resume")


def native_session_in_argv(argv: list[str], session_id: str) -> bool:
    """The producer process itself names the native session: ``--resume <id>``, ``--session-id <id>``, ``resume <id>`` or the
    ``=`` forms. A transcript that merely exists on disk, or an id in the config, is not this relation."""
    for i, tok in enumerate(argv):
        if tok.partition("=")[0] in NATIVE_FLAGS and tok.partition("=")[2] == session_id:
            return True
        if i and argv[i - 1] in NATIVE_FLAGS and tok == session_id:
            return True
    return False


def observe_local(ident: QAIdentity, pin, runner_credential, known, api=None, info=None, argv_of=None,
                  local_host: str | None = None, clock=now_epoch) -> dict:
    """Supported READ-ONLY proof of the actual target and producer, independently obtained NOW. Never a config echo.

    Observed from the machine: this host's name; the iTerm session id and its tty (iTerm's own dictionary); the producer
    pid, start time, tty and foreground group (ps); the native session id as named by the producer's own argv, plus its
    transcript; the runner id of the registered runner credential. Anything missing, ambiguous, stale or different from the
    admitted ``ident`` is a typed ``QARefusal`` and nothing else happens. It injects nothing, writes no ledger/journal,
    takes no lock, and never creates, adopts or restarts a producer.

    ``runner_credential`` is ``() -> runner_id`` (the already registered credential, read only); ``known(session_id)`` is the
    transcript probe. The result carries ``provenance`` per target field: ``observed`` (read from the machine here) or
    ``backend`` (workspace_id, agent_id and host_id have no local source; the authenticated ``/check`` binds them)."""
    api, info, argv_of = api or it.ITermAPI(), info or it.process_info, argv_of or it.process_argv
    t = ident.target
    if not it.host_matches(t["hostname"], local_host):
        raise QARefusal("host_mismatch")
    reason = it.check_iterm(api, pin, info)
    if reason:
        raise QARefusal(reason)
    tty, why = api.probe(pin.iterm_session_id)
    if why:
        raise QARefusal(why)
    pgid, tpgid, ps_tty, started = info(pin.producer_pid)
    producer = {"transport": "iterm", "terminal_session_id": pin.iterm_session_id, "tty": tty,
                "producer_pid": pin.producer_pid, "producer_started": started}
    if "/dev/" + ps_tty != tty or pgid != tpgid:
        raise QARefusal("producer_not_foreground")
    if producer != ident.producer:
        raise QARefusal("producer_mismatch")
    if not native_session_in_argv(argv_of(pin.producer_pid), t["session_id"]):
        raise QARefusal("native_relation_unproven")
    try:
        if not known(t["session_id"]):
            raise QARefusal("session_unknown")
        runner_id = runner_credential()
    except QARefusal:
        raise
    except Exception as e:
        raise QARefusal("runner_credential_unreadable", type(e).__name__) from None
    if runner_id != t["runner_id"]:
        raise QARefusal("runner_id_mismatch")
    target = dict(t, hostname=t["hostname"])
    prov = {k: "observed" for k in ("hostname", "session_id", "runner_id")}
    prov.update({k: "backend" for k in ("workspace_id", "agent_id", "host_id")})
    return {"target": target, "producer": producer, "provenance": prov, "observed_at": utc(clock())}


# ---------------------------------------------------------------------- host-local guard (documented supported helper)
def find_record(journal_dir: Path, delivery_id: str) -> dict | None:
    if not md.is_uuid(delivery_id):
        return None
    for p in sorted(Path(journal_dir).glob("*.json")):
        rec = md.Journal(journal_dir).get(p.stem)
        if rec and rec.get("capability") == CAPABILITY and rec.get("delivery_id") == delivery_id:
            return rec
    return None


def make_observer(ident: QAIdentity, pin, runner_credential, known, api=None, info=None, argv_of=None,
                  local_host: str | None = None, clock=now_epoch):
    """``() -> observation`` over ``observe_local`` with every dependency fixed: the callback a guard or receiver uses."""
    return lambda: observe_local(ident, pin, runner_credential, known, api, info, argv_of, local_host, clock)


def _same_binding(a: dict, b: dict) -> bool:
    return a["target"] == b["target"] and a["producer"] == b["producer"]


def _record_shape(rec: dict) -> None:
    """A journal record is untrusted disk state: every field the guard reads must be present and of the right type, else the
    typed refusal ``delivery_record_malformed`` (never a traceback, never a half-read authorisation)."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    q = rec.get("qa")
    ok = (isinstance(q, dict) and isinstance(rec.get("state"), str) and md.is_uuid(rec.get("delivery_id"))
          and md.is_uuid(rec.get("request_id")) and md._is_sha256(rec.get("text_sha256"))
          and md.is_uuid(q.get("receiver_id")) and num(q.get("admission_revision")) and num(q.get("expires"))
          and isinstance(q.get("policy_sha256"), str) and isinstance(q.get("target"), dict)
          and isinstance(q.get("producer"), dict))
    if not ok:
        raise QARefusal("delivery_record_malformed")


def guard(settings: QASettings, ledger_root: Path, journal_dir: Path, client: QAClient, delivery_id: str, boundary: str,
          observe, clock=now_epoch) -> dict:
    """Authorise ONE normal mutation of the operation that consumes a delivered assignment. Read-only: it independently
    observes the actual target and producer (``observe_local``) BEFORE asking the live backend (``/check``) and AGAIN after
    the answer; the two observations must be identical and equal to the delivery's binding. It executes nothing, writes no
    ledger record and needs no lock. Call it before EACH mutation (QA stop/start, each asset replacement, channel start, each
    controller lifecycle request); an answer is valid for that one call, never cached. Raises ``QARefusal`` otherwise.
    Returns the observed target + producer so the caller can record exactly what was proven."""
    if boundary not in BOUNDARIES:
        raise QARefusal("unknown_boundary")
    ident = settings.identity
    if clock() >= ident.expires:
        raise QARefusal(ERR_EXPIRED)
    rec = find_record(journal_dir, delivery_id)
    if rec is None or not rec.get("qa"):
        raise QARefusal("delivery_unknown")
    _record_shape(rec)
    if rec["state"] not in ("received", "acknowledged"):
        raise QARefusal("delivery_not_delivered", rec["state"])
    q = rec["qa"]
    if q["target"] != ident.target or q["producer"] != ident.producer or q["policy_sha256"] != pinned()["policy_sha256"]:
        raise QARefusal("delivery_binding_mismatch")
    try:
        ids = [i["receiver_id"] for i in read_admissions(ledger_root, ident)]
    except QARefusal:
        raise QARefusal(ERR_REVOKED) from None
    if q["receiver_id"] not in ids:
        raise QARefusal(ERR_REVOKED)
    before = observe()
    if before["target"] != q["target"] or before["producer"] != q["producer"]:
        raise QARefusal("observed_binding_mismatch")
    request = {"receiver_id": q["receiver_id"], "admission_revision": q["admission_revision"], "delivery_id": rec["delivery_id"],
               "request_id": rec["request_id"], "capability": CAPABILITY, "policy_sha256": q["policy_sha256"],
               "text_sha256": rec["text_sha256"], "target": q["target"], "producer": q["producer"], "expires": q["expires"]}
    got = client.check(request)
    after = observe()                                        # the HTTP round trip is not a reason to trust the earlier look
    if not _same_binding(before, after):
        raise QARefusal("binding_changed_during_check")
    return {"ok": True, "boundary": boundary, "delivery_id": rec["delivery_id"], "request_id": rec["request_id"],
            "receiver_id": q["receiver_id"], "admission_revision": q["admission_revision"], "expires": q["expires"],
            "checked_at": got["checked_at"], "target": after["target"], "producer": after["producer"],
            "provenance": after["provenance"], "observed_at": after["observed_at"]}


def revoke(settings: QASettings, ledger_root: Path, client: QAClient) -> list[str]:
    """Disable: revoke ONLY the QA admissions this receiver recorded. Touches neither the legacy admission, the journal, the
    ledger pin, the producer nor the session. A server answer other than 204 keeps the record (retry is safe)."""
    ident = settings.identity
    done = []
    with _AdmissionLock(ledger_root):
        if (Path(ledger_root) / REVOKED_FILE).exists() and not (Path(ledger_root) / ADMISSION_FILE).exists():
            return done                                      # already fully revoked: a repeat is a successful no-op
        items = read_admissions(ledger_root, ident)
        left = list(items)
        for item in reversed(items):
            client.revoke(item["receiver_id"])               # raises on anything but 204: record stays for the retry
            left.remove(item)
            done.append(item["receiver_id"])
            if left:
                write_admissions(ledger_root, ident, left)
        (Path(ledger_root) / ADMISSION_FILE).replace(Path(ledger_root) / REVOKED_FILE)
    return done

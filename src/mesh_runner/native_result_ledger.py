"""Durable native-result mailbox; no Runner, Mesh, process or lease mutations.

This is transport state, NOT a native identity authenticator. A source adapter
must authenticate the original session before calling publish(). A receiver
adapter must freshly check task/attempt/session/generation before delivery and
ack(). Neither an event nor a transport ACK is an operational approval.

One private directory per integration. JSON replacement + directory fsync is
the commit boundary; flock serializes writers. Pending delivery survives restart
and is repeated with the SAME id until the exact receiver acknowledges it.
No clock, timers, runner.sqlite access, comments, NEXT, or task status effects.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def uuid(value):
    if not isinstance(value, str):
        raise Refused("UUID required")
    try:
        parsed = str(UUID(value))
    except ValueError:
        raise Refused("UUID required") from None
    if parsed != value:
        raise Refused("canonical UUID required")
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Refused("SHA256 required")
    return value


def positive(value):
    if type(value) is not int or value < 1:
        raise Refused("positive integer required")
    return value


def fields(value, wanted):
    if not isinstance(value, dict) or set(value) != set(wanted):
        raise Refused("unexpected or missing fields")


def receiver(value):
    fields(value, ("task", "attempt", "session", "generation"))
    return {k: positive(v) if k == "generation" else uuid(v) for k, v in value.items()}


def binding(value):
    fields(value, ("producer", "task", "stage", "package_sha256", "test"))
    uuid(value["producer"])
    uuid(value["task"])
    digest(value["package_sha256"])
    if (not isinstance(value["stage"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value["stage"])):
        raise Refused("invalid stage")
    if type(value["test"]) is not bool:
        raise Refused("explicit test marker required")
    return dict(value)


def event(value):
    fields(value, ("binding", "sequence", "status", "receipt_sha256", "owner", "reason"))
    binding(value["binding"])
    positive(value["sequence"])
    digest(value["receipt_sha256"])
    if value["status"] not in ("result", "failed", "dead", "unknown"):
        raise Refused("invalid result status")
    if (not isinstance(value["owner"], str) or not value["owner"].strip()
            or len(value["owner"]) > 128 or not isinstance(value["reason"], str)
            or len(value["reason"]) > 512):
        raise Refused("bounded owner/reason required")
    if value["status"] != "result" and not value["reason"].strip():
        raise Refused("terminal failure needs an explicit reason")
    return json.loads(canonical(value))


class Ledger:
    """Fail closed on missing/corrupt state; constructor never creates state.

    Explicit initialize() is the only empty-store creation path. Losing a cursor
    cannot silently become a fresh registration. No automatic retention/deletion.
    """
    def __init__(self, directory):
        self.root = Path(directory)

    @classmethod
    def initialize(cls, directory):
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        store = cls(root)
        with store._locked():
            store._save({"version": 1, "registrations": {}, "events": {}, "highwater": {}})
        return store

    def _check_root(self):
        st = self.root.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused("private owned state directory required")

    @contextmanager
    def _locked(self):
        self._check_root()
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise Refused("private owned lock required")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        fd = os.open(self.root / "state.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise Refused("private owned state required")
            try:
                state = json.load(stream)
            except (ValueError, UnicodeError):
                raise Refused("state corrupt; recovery required") from None
        fields(state, ("version", "registrations", "events", "highwater"))
        if state["version"] != 1 or any(not isinstance(state[k], dict)
                                       for k in ("registrations", "events", "highwater")):
            raise Refused("state corrupt; recovery required")
        return state

    def _save(self, state):
        fd, name = tempfile.mkstemp(prefix="commit-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "state.json")
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(name).unlink(missing_ok=True)

    def register(self, source, target):
        source, target = binding(source), receiver(target)
        rid = identity({"binding": source, "receiver": target})
        with self._locked():
            state = self._load()
            if rid not in state["registrations"]:
                state["registrations"][rid] = {"binding": source, "receiver": target,
                                               "cursor": 0, "pending": None}
                self._save(state)
            return {"registration": rid, **state["registrations"][rid]}

    def readback(self, registration):
        digest(registration)
        with self._locked():
            state = self._load()
            if registration not in state["registrations"]:
                raise Refused("registration absent")
            return {"registration": registration, **state["registrations"][registration]}

    def publish(self, value, authenticated_producer, authenticated_task):
        """Authenticated IDs are adapter inputs, never copied from the frame.

        Exact duplicate retry returns its id without another insertion; a lower
        sequence or a sequence reused for different bytes is refused. The
        channel key excludes test so test/real cannot independently replay it.
        """
        value = event(value)
        source = value["binding"]
        if source["producer"] != uuid(authenticated_producer) or source["task"] != uuid(authenticated_task):
            raise Refused("authenticated producer/task mismatch")
        eid = identity(value)
        channel = identity({k: source[k] for k in ("producer", "task", "stage")})
        with self._locked():
            state = self._load()
            if eid in state["events"]:
                return {"event": eid, "duplicate": True}
            if not any(r["binding"] == source for r in state["registrations"].values()):
                raise Refused("no exact registration")
            if value["sequence"] <= state["highwater"].get(channel, 0):
                raise Refused("stale or reused sequence")
            state["events"][eid] = value
            state["highwater"][channel] = value["sequence"]
            self._save(state)
            return {"event": eid, "duplicate": False}

    def delivery(self, registration, fresh_receiver):
        """Prepare/repeat outbox; this is NOT receiver consumption or readiness."""
        fresh_receiver = receiver(fresh_receiver)
        digest(registration)
        with self._locked():
            state = self._load()
            row = state["registrations"].get(registration)
            if row is None or row["receiver"] != fresh_receiver:
                raise Refused("receiver identity changed")
            if row["pending"] is None:
                available = [(e["sequence"], eid) for eid, e in state["events"].items()
                             if e["binding"] == row["binding"] and e["sequence"] > row["cursor"]]
                if not available:
                    return None
                row["pending"] = min(available)[1]
                self._save(state)
            eid = row["pending"]
            return {"registration": registration, "event": eid, "receiver": row["receiver"],
                    "value": state["events"][eid]}

    def ack(self, registration, eid, fresh_receiver):
        """Only exact pending event + receiver advances cursor. Safe ACK retry."""
        digest(registration)
        digest(eid)
        fresh_receiver = receiver(fresh_receiver)
        with self._locked():
            state = self._load()
            row = state["registrations"].get(registration)
            value = state["events"].get(eid)
            if row is None or row["receiver"] != fresh_receiver or value is None:
                raise Refused("ACK identity mismatch")
            if value["binding"] != row["binding"]:
                raise Refused("ACK binding mismatch")
            if row.get("last_ack") == eid:
                return {"event": eid, "cursor": row["cursor"], "duplicate": True}
            if row["pending"] != eid or value["sequence"] <= row["cursor"]:
                raise Refused("ACK is not pending")
            row.update(cursor=value["sequence"], pending=None, last_ack=eid)
            self._save(state)
            return {"event": eid, "cursor": row["cursor"], "duplicate": False}

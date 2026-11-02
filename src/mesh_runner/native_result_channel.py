"""Authenticated stdio bridge between enrolled source and receiver ledger.

The operator bootstraps on the original source machine, then transfers that
private bootstrap to the receiver. Possession of this channel's random key is
an operator trust boundary, not proof supplied by a Mesh author or uploaded log.
SSH host/user verification belongs to the explicitly configured command. No
shell, network listener, credential output, installation or Runner mutation.

Source failures use a separate observer identity/registration. They never emit
as the original native producer and never consume its sequence numbers.
"""
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

from .native_result_ledger import (Ledger, Refused, binding, canonical, event,
                                  digest, fields, identity, receiver, uuid)
from .native_result_source import enroll, scan
from . import native_result_process as process_fence
from .native_result_lineage import source_identity, validate as lineage_validate, verify_local

MAX_MESSAGE = 2 * 1024 * 1024


class Unavailable(RuntimeError):
    """No authenticated commit reply; leave the source cursor unchanged."""


def private_root(root):
    root = Path(root)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise Refused("private owned channel directory required")
    return root


def read_private(path, limit=MAX_MESSAGE):
    path = Path(path)
    private_root(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise Refused("private owned channel file required")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Refused("channel file exceeds bound")
    return raw


def save_private(path, raw):
    path = Path(path)
    private_root(path.parent)
    fd, name = tempfile.mkstemp(prefix="commit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(root):
    root = private_root(root)
    fd = os.open(root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise Refused("private owned channel lock required")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def descriptor(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise Refused("unsupported channel version")
    names = ("version", "source", "receiver", "observer", "owner")
    wanted = (*names, "lineage", "process") if value["version"] == 3 else (
        (*names, "lineage") if value["version"] == 2 else names)
    fields(value, wanted)
    if value["version"] not in (1, 2, 3):
        raise Refused("unsupported channel version")
    receiver(value["receiver"])
    uuid(value["observer"])
    uuid(value["owner"])
    source = source_identity(value["source"])
    producers = {source["binding"]["producer"]}
    if value["version"] == 2 and value["lineage"] is None:
        raise Refused("successor channel requires lineage")
    if value.get("lineage") is not None:
        lineage_validate(value["lineage"], source)
        if value["owner"] != value["lineage"]["operator"]:
            raise Refused("successor operator differs from channel owner")
        # Validation bounds the whole chain. A later same-file stage must not
        # hide the original producer behind an intermediate successor source.
        lineage = value["lineage"]
        while lineage is not None:
            producers.update(lineage[side]["binding"]["producer"]
                             for side in ("previous", "successor"))
            lineage = lineage.get("previous_lineage")
    if value["version"] == 3:
        pin = value["process"]
        fields(pin, ("identity", "source_sha256", "operator", "authority_sha256"))
        process_fence.validate(pin["identity"])
        digest(pin["authority_sha256"])
        if pin["source_sha256"] != identity(source) or pin["operator"] != value["owner"]:
            raise Refused("native process enrollment scope changed")
    elif not source["binding"]["test"]:
        raise Refused("production channel requires an explicit native process fence")
    if value["observer"] in producers:
        raise Refused("observer must be distinct from native producer")
    return value


def load(root):
    root = private_root(root)
    desc = descriptor(json.loads(read_private(root / "channel.json")))
    key = read_private(root / "key", 32)
    if len(key) != 32:
        raise Refused("channel key corrupt")
    return desc, key


def bootstrap(directory, journal, target, observer, owner, *, process=None, authority_sha256=None):
    """Explicit source-machine enrollment. Never enroll from a copied upload."""
    state = journal.enrollment()
    source = state["source"]
    original = enroll(source["root"], source["source"], source["binding"])
    if any(source[k] != original[k] for k in original if k != "cursor"):
        raise Refused("source enrollment changed")
    scan(source)  # Validate the persisted cursor without consuming or publishing.
    desc = {"version": 1, "source": source, "receiver": receiver(target),
            "observer": uuid(observer), "owner": uuid(owner)}
    if state["version"] == 2:
        desc.update(version=2, lineage=verify_local(state["lineage"], source))
    if process is not None:
        process_fence.verify(process)
        desc.update(version=3, lineage=desc.get("lineage"),
                    process={"identity": process, "source_sha256": identity(source),
                             "operator": uuid(owner), "authority_sha256": digest(authority_sha256)})
    elif authority_sha256 is not None:
        raise Refused("native process pin missing")
    desc = descriptor(desc)
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    save_private(root / "channel.json", canonical(desc))
    save_private(root / "key", secrets.token_bytes(32))
    return identity(desc)


def failure_binding(desc):
    return binding({"producer": desc["observer"], "task": desc["source"]["binding"]["task"],
                    "stage": "source-failure", "package_sha256": identity(desc),
                    "test": desc["source"]["binding"]["test"]})


def install(directory, ledger, bootstrap_directory):
    """Receiver-side explicit install from an operator-transferred bootstrap.

    Private bootstrap transfer is deliberately outside this module. An existing
    channel cannot be silently overwritten, rebound or repaired by this method.
    """
    desc, key = load(bootstrap_directory)
    native = ledger.register(desc["source"]["binding"], desc["receiver"])["registration"]
    failure = ledger.register(failure_binding(desc), desc["receiver"])["registration"]
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    save_private(root / "channel.json", canonical(desc))
    save_private(root / "key", key)
    save_private(root / "endpoint.json", canonical({"ledger": str(ledger.root.resolve()),
                 "native": native, "failure": failure}))
    return {"channel": identity(desc), "native": native, "failure": failure}


def signed(body, key):
    return {"body": body, "mac": hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


def verified(envelope, key):
    fields(envelope, ("body", "mac"))
    if (not isinstance(envelope["mac"], str)
            or not hmac.compare_digest(signed(envelope["body"], key)["mac"], envelope["mac"])):
        raise Refused("channel authentication refused")
    return envelope["body"]


def failure_event(desc, evidence):
    fields(evidence, ("channel", "owner", "status", "operation", "error", "reason"))
    if (evidence["channel"] != identity(desc) or evidence["owner"] != desc["owner"]
            or evidence["status"] != "unknown"):
        raise Refused("observer failure scope mismatch")
    for name, limit in (("operation", 64), ("error", 64), ("reason", 512)):
        if (not isinstance(evidence[name], str) or not evidence[name].strip()
                or len(evidence[name]) > limit):
            raise Refused("bounded observer grounds required")
    return event({"binding": failure_binding(desc), "sequence": 1, "status": "unknown",
                  "receipt_sha256": identity(evidence), "owner": desc["owner"],
                  "reason": (evidence["operation"] + ": " + evidence["error"]
                             + ": " + evidence["reason"])[:512]})


def failure_delivery(channels, ledger, registration, current):
    """Controller read-only binding of a primary WAIT to its observer channel.

    The receiver operator installs the endpoint at channels/<native-registration>.
    Missing or changed metadata is an integration refusal, never silence or a
    fallback to comments. Failure evidence precedes its ledger event, so a crash
    between those commits refuses until the source resends the same evidence.
    """
    root = Path(channels) / registration
    # Registration is used as one path component only.
    digest(registration)
    desc, _ = load(root)
    endpoint = json.loads(read_private(root / "endpoint.json"))
    fields(endpoint, ("ledger", "native", "failure"))
    primary = ledger.readback(registration)
    if (endpoint["native"] != registration or Path(endpoint["ledger"]) != ledger.root.resolve()
            or desc["receiver"] != receiver(current)
            or primary["receiver"] != current or primary["binding"] != desc["source"]["binding"]):
        raise Refused("observer channel registration changed")
    failed = ledger.readback(endpoint["failure"])
    if failed["binding"] != failure_binding(desc) or failed["receiver"] != current:
        raise Refused("observer failure registration changed")
    packet = ledger.delivery(endpoint["failure"], current)
    try:
        evidence = json.loads(read_private(root / "failure.json"))
    except FileNotFoundError:
        if packet is not None or failed["cursor"] or failed["pending"]:
            raise Refused("observer failure evidence lost") from None
        return None
    expected = failure_event(desc, evidence)
    if packet is None or packet["value"] != expected:
        raise Refused("observer failure awaiting durable delivery or explicit repair")
    return packet


def receive(root, envelope):
    """One authenticated request. Successful reply follows durable ledger commit."""
    root = Path(root)
    with locked(root):
        desc, key = load(root)
        body = verified(envelope, key)
        fields(body, ("version", "channel", "kind", "value"))
        if type(body["version"]) is not int or body["version"] != 1 or body["channel"] != identity(desc):
            raise Refused("channel scope mismatch")
        endpoint = json.loads(read_private(root / "endpoint.json"))
        fields(endpoint, ("ledger", "native", "failure"))
        ledger = Ledger(endpoint["ledger"])
        if body["kind"] == "native":
            if (root / "failure.json").exists():
                raise Refused("source failure latched; explicit recovery required")
            value = event(body["value"])
            if value["binding"] != desc["source"]["binding"]:
                raise Refused("native channel binding mismatch")
            rid = endpoint["native"]
        elif body["kind"] == "failure":
            value = failure_event(desc, body["value"])
            rid = endpoint["failure"]
            if (root / "failure.json").exists():
                if json.loads(read_private(root / "failure.json")) != body["value"]:
                    raise Refused("different terminal failure; explicit recovery required")
            else:
                # Evidence commits before its event; restart repairs a crash here.
                save_private(root / "failure.json", canonical(body["value"]))
        else:
            raise Refused("unsupported channel message")
        row = ledger.readback(rid)
        if row["binding"] != value["binding"] or row["receiver"] != desc["receiver"]:
            raise Refused("receiver channel registration changed")
        receipt = ledger.publish(value, value["binding"]["producer"], value["binding"]["task"])
        return signed({"version": 1, "channel": identity(desc), "request": identity(body),
                       "receipt": receipt}, key)


class RemoteSink:
    def __init__(self, directory, command):
        self.root = private_root(directory)
        self.desc, self.key = load(self.root)
        if (not isinstance(command, list) or not command
                or any(not isinstance(x, str) or not x or "\x00" in x for x in command)
                or not Path(command[0]).is_absolute()):
            raise Refused("explicit absolute receiver command required")
        self.command = command

    def _exchange(self, kind, value, expected):
        body = {"version": 1, "channel": identity(self.desc), "kind": kind, "value": value}
        raw = canonical(signed(body, self.key))
        if len(raw) > MAX_MESSAGE:
            raise Refused("channel request exceeds bound")
        try:
            result = subprocess.run(self.command, input=raw, capture_output=True,
                                    timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unavailable("receiver command unavailable; cursor retained") from None
        if result.returncode or len(result.stdout) > MAX_MESSAGE:
            raise Unavailable("no successful receiver commit reply; cursor retained")
        try:
            reply = verified(json.loads(result.stdout), self.key)
            fields(reply, ("version", "channel", "request", "receipt"))
            fields(reply["receipt"], ("event", "duplicate"))
            if (type(reply["version"]) is not int or reply["version"] != 1
                    or reply["channel"] != identity(self.desc) or reply["request"] != identity(body)
                    or reply["receipt"]["event"] != identity(expected)
                    or type(reply["receipt"]["duplicate"]) is not bool):
                raise Refused("receiver commit reply scope mismatch")
        except (ValueError, TypeError, KeyError):
            raise Unavailable("invalid receiver commit reply; cursor retained") from None
        return reply["receipt"]

    def publish(self, value, authenticated_producer, authenticated_task):
        value = event(value)
        contract = self.desc["source"]["binding"]
        if (value["binding"] != contract or authenticated_producer != contract["producer"]
                or authenticated_task != contract["task"]):
            raise Refused("source channel producer/task mismatch")
        if (self.root / "failure.json").exists():
            raise Refused("source failure latched; explicit recovery required")
        return self._exchange("native", value, value)

    def fail(self, operation, error, reason):
        # First failure is sticky, also across process death or lost replies.
        with locked(self.root):
            path = self.root / "failure.json"
            if path.exists():
                evidence = json.loads(read_private(path))
            else:
                evidence = {"channel": identity(self.desc), "owner": self.desc["owner"],
                            "status": "unknown", "operation": operation,
                            "error": error, "reason": reason}
                failure_event(self.desc, evidence)
                save_private(path, canonical(evidence))
        return self._exchange("failure", evidence, failure_event(self.desc, evidence))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["receive"])
    parser.add_argument("--channel", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise Refused("channel request exceeds bound")
        reply = receive(args.channel, json.loads(raw))
        sys.stdout.buffer.write(canonical(reply) + b"\n")
    except (OSError, ValueError, TypeError, KeyError):
        # Never echo the request, private config, key, source frame or traceback.
        print("native-result endpoint refused request", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

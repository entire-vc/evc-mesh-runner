"""Enrolled immutable reviewer-receipt source for the existing native channel.

The reviewer receipt (schema native-review-admission/v1) is issued by the Mesh
side (A1). This module only transports it: the publisher delivers bytes, it
never issues or alters a verdict, and a transport status=result means "data
delivered", NEVER admission or execute permission. No budget, reservation,
clock or t0 exists here.

Trust root: an operator-pinned enrollment in the channel descriptor (version 4).
Every identity/scope/pin/receiver/native binding of a packet must equal that
pin; only the fields in FREE may vary. Unknown or missing field => Refused.
The receipt hash never contains the native event id: the event is created AFTER
the receipt and pins receipt_sha256; an append-only linkage records
registration + event + receipt id.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .native_result_ledger import (Refused, binding, canonical, digest, event, fields,
                                    identity, positive, receiver, uuid)

SCHEMA = "native-review-admission/v1"
DOMAIN = "t12-successor-synthetic"
MAX_PACKET = 2 * 1024 * 1024 - 8192  # leaves room for the channel envelope
HEX32 = re.compile(r"[0-9a-f]{32}")
GIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z")

RECEIPT_FIELDS = ("schema", "receipt_id", "enrollment_id", "authority_id", "product",
                  "issuer_actor_id", "publisher_actor_id", "transport_source_id",
                  "result_author_actor_id", "review_task_id", "source_task_id",
                  "workspace_id", "project_id", "owner_actor_id", "reviewer_family",
                  "author_family", "verdict", "scope", "pins", "receiver", "native",
                  "body")
PACKET_FIELDS = (*RECEIPT_FIELDS, "receipt_sha256", "created_at", "issuer_signature")
SIGN_DOMAIN = b"native-review-admission/v1\n"
UUID_FIELDS = ("receipt_id", "enrollment_id", "authority_id", "issuer_actor_id",
               "publisher_actor_id", "transport_source_id", "result_author_actor_id",
               "review_task_id", "source_task_id", "workspace_id", "project_id",
               "owner_actor_id")
# The only values the enrollment does not fix in advance.
FREE = ("receipt_id", "result_author_actor_id", "verdict", "body")


def _family(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
        raise Refused("invalid model family")
    return value


def unsigned(packet):
    """The immutable receipt fields that receipt_sha256 covers."""
    return {k: packet[k] for k in RECEIPT_FIELDS}


def receipt_digest(packet):
    return hashlib.sha256(canonical(unsigned(packet))).hexdigest()


def decode_body(body):
    """Return the exact original reviewer bytes or refuse; never truncates."""
    fields(body, ("encoding", "bytes", "sha256"))
    if body["encoding"] != "base64" or not isinstance(body["bytes"], str):
        raise Refused("unsupported review body encoding")
    digest(body["sha256"])
    try:
        raw = base64.b64decode(body["bytes"].encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        raise Refused("review body is not canonical base64") from None
    if base64.b64encode(raw).decode() != body["bytes"]:
        raise Refused("review body is not canonical base64")
    if not raw:
        raise Refused("review body bytes required (digest-only refused)")
    if hashlib.sha256(raw).hexdigest() != body["sha256"]:
        raise Refused("review body bytes do not match their digest")
    return raw


def signature_bytes(value):
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (AttributeError, binascii.Error, UnicodeError, ValueError):
        raise Refused("issuer signature must be canonical base64") from None
    if len(raw) != 64 or base64.b64encode(raw).decode() != value:
        raise Refused("issuer signature must be a 64-byte Ed25519 signature")
    return raw


def issuer_key(value):
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (AttributeError, binascii.Error, UnicodeError, ValueError):
        raise Refused("issuer public key must be canonical base64") from None
    if len(raw) != 32 or base64.b64encode(raw).decode() != value:
        raise Refused("issuer public key must be 32 bytes")
    return raw


def signing_message(packet):
    return SIGN_DOMAIN + packet["receipt_sha256"].encode("ascii")


def verify_issuer(packet, key):
    """Only the enrolled issuer key can make a receipt; the publisher/channel key cannot."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise Refused("issuer verification unavailable: cryptography required") from None
    try:
        Ed25519PublicKey.from_public_bytes(issuer_key(key)).verify(
            signature_bytes(packet["issuer_signature"]), signing_message(packet))
    except InvalidSignature:
        raise Refused("issuer signature does not verify against the enrolled issuer key") from None


def validate(packet):
    """Closed structural validation of one receipt packet; no trust decisions."""
    fields(packet, PACKET_FIELDS)
    if len(canonical(packet)) > MAX_PACKET:
        raise Refused("receipt packet exceeds channel bound; no truncation")
    if packet["schema"] != SCHEMA:
        raise Refused("unsupported receipt schema")
    for name in UUID_FIELDS:
        uuid(packet[name])
    product = packet["product"]
    fields(product, ("owning_task_id", "observation_id", "session_id",
                     "authority_sha256", "root_disposition_sha256"))
    uuid(product["owning_task_id"])
    for name in ("observation_id", "session_id"):
        if not isinstance(product[name], str) or not HEX32.fullmatch(product[name]):
            raise Refused("product ids are 32 lowercase hex")
    digest(product["authority_sha256"])
    digest(product["root_disposition_sha256"])
    _family(packet["reviewer_family"])
    _family(packet["author_family"])
    if packet["verdict"] not in ("SHIP", "DO-NOT-SHIP"):
        raise Refused("invalid verdict")
    scope = packet["scope"]
    fields(scope, ("domain", "namespace", "test", "criteria_sha256", "context_sha256"))
    if scope["domain"] != DOMAIN or scope["test"] is not True:
        raise Refused("only the synthetic test scope is supported")
    uuid(scope["namespace"])
    digest(scope["criteria_sha256"])
    digest(scope["context_sha256"])
    pins = packet["pins"]
    fields(pins, ("source_repo", "source_sha", "package_sha256", "manifest_sha256",
                  "config_sha256"))
    if not isinstance(pins["source_repo"], str) or not REPO.fullmatch(pins["source_repo"]):
        raise Refused("invalid source repository")
    if not isinstance(pins["source_sha"], str) or not GIT_ID.fullmatch(pins["source_sha"]):
        raise Refused("full source sha required")
    for name in ("package_sha256", "manifest_sha256", "config_sha256"):
        digest(pins[name])
    receiver(packet["receiver"])
    native = packet["native"]
    fields(native, ("registration", "binding"))
    digest(native["registration"])
    binding(native["binding"])
    decode_body(packet["body"])
    digest(packet["receipt_sha256"])
    signature_bytes(packet["issuer_signature"])
    if not isinstance(packet["created_at"], str) or not TIME.fullmatch(packet["created_at"]):
        raise Refused("server UTC timestamp required")
    if receipt_digest(packet) != packet["receipt_sha256"]:
        raise Refused("receipt digest does not match its fields")
    return packet


def _get(value, path):
    for part in path.split("."):
        value = value[part]
    return value


def _without_free(packet):
    value = json.loads(canonical(unsigned(packet)))
    for name in FREE:
        value.pop(name)
    return value


def enrollment(value):
    """Operator-pinned expectation. Same closed shape as a packet minus FREE."""
    fields(value, ("pin", "assignment", "issuer_key"))
    issuer_key(value["issuer_key"])
    pin = value["pin"]
    # Validate shape by completing the free fields with a placeholder packet.
    probe = json.loads(canonical(pin))
    probe.update(receipt_id=pin.get("enrollment_id"), result_author_actor_id=pin.get("owner_actor_id"),
                 verdict="SHIP", body={"encoding": "base64", "bytes": "AA==",
                                        "sha256": hashlib.sha256(b"\x00").hexdigest()})
    probe.update(receipt_sha256="0" * 64, created_at="2026-01-01T00:00:00Z",
                 issuer_signature=base64.b64encode(b"\x00" * 64).decode())
    probe["receipt_sha256"] = receipt_digest(probe)
    validate(probe)
    if pin["issuer_actor_id"] == pin["publisher_actor_id"]:
        raise Refused("publisher must differ from issuer")
    if pin["issuer_actor_id"] == pin["transport_source_id"]:
        raise Refused("transport source must differ from issuer")
    if pin["reviewer_family"] == pin["author_family"]:
        raise Refused("reviewer family must differ from author family")
    if pin["native"]["binding"]["test"] is not True:
        raise Refused("receipt source supports test namespace only")
    # The authenticated source assignment is the native producer, never a frame field.
    if pin["native"]["binding"]["producer"] != pin["transport_source_id"]:
        raise Refused("native producer must be the enrolled transport source")
    if pin["native"]["binding"]["task"] != pin["source_task_id"]:
        raise Refused("native task must be the enrolled source task")
    if pin["receiver"] != receiver(pin["receiver"]):
        raise Refused("receiver pin changed")
    fields(value["assignment"], ("owner", "sequence"))
    uuid(value["assignment"]["owner"])
    if value["assignment"]["sequence"] != 1:
        raise Refused("one receipt per enrollment")
    return value


def admit_packet(packet, enrolled):
    """Trusted-source gate. Anything not exactly pinned is refused."""
    validate(packet)
    enrollment(enrolled)
    if _without_free(packet) != enrolled["pin"]:
        raise Refused("receipt does not match the enrolled source pin")
    if packet["issuer_actor_id"] == packet["publisher_actor_id"]:
        raise Refused("publisher must differ from issuer")
    verify_issuer(packet, enrolled["issuer_key"])
    return packet


def native_event(packet, enrolled):
    """Native frame for the existing ledger: data delivery, never admission."""
    admit_packet(packet, enrolled)
    return event({"binding": enrolled["pin"]["native"]["binding"],
                  "sequence": enrolled["assignment"]["sequence"], "status": "result",
                  "receipt_sha256": packet["receipt_sha256"],
                  "owner": enrolled["assignment"]["owner"], "reason": ""})


# ---- receiver-side append-only store + linkage -------------------------------

def _private_dir(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("private owned receipt directory required")
    return Path(path)


def _write_once(path, raw):
    """O_EXCL create; an identical existing file is a duplicate, different bytes refuse."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read(path) != raw:
            raise Refused("append-only record already holds different bytes") from None
        return True
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return False


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused("private owned receipt file required")
        return stream.read(MAX_PACKET + 1)


def store(root, packet):
    """Persist the full receipt bytes before the native event is published."""
    directory = _private_dir(Path(root) / "receipts")
    return _write_once(directory / (packet["receipt_sha256"] + ".json"), canonical(packet))


def link(root, registration, event_id, packet):
    """Append-only registration+event+receipt linkage, written after publish."""
    directory = _private_dir(Path(root) / "receipts")
    record = {"registration": digest(registration), "event": digest(event_id),
              "receipt_id": uuid(packet["receipt_id"]),
              "receipt_sha256": packet["receipt_sha256"]}
    return _write_once(directory / (packet["receipt_sha256"] + ".link"), canonical(record))


def load(root, delivered, enrolled):
    """Ordinary consumer read: full verified BODY + bindings for a delivered packet.

    The result is DATA. `admitted` is always False; reserving/executing needs the
    separate Mesh-side admission, never a transport status or ACK.
    """
    directory = _private_dir(Path(root) / "receipts")
    value = delivered["value"]
    sha = digest(value["receipt_sha256"])
    if value["status"] != "result" or value["binding"] != enrolled["pin"]["native"]["binding"]:
        raise Refused("delivered event is not this enrolled receipt")
    packet = validate(json.loads(_read(directory / (sha + ".json"))))
    admit_packet(packet, enrolled)
    linked = json.loads(_read(directory / (sha + ".link")))
    fields(linked, ("registration", "event", "receipt_id", "receipt_sha256"))
    if (linked["event"] != delivered["event"] or linked["registration"] != delivered["registration"]
            or linked["receipt_sha256"] != sha or linked["receipt_id"] != packet["receipt_id"]
            or delivered["receiver"] != packet["receiver"]
            or delivered["registration"] != packet["native"]["registration"]
            or identity(value) != delivered["event"]):
        raise Refused("receipt linkage does not match the delivered event")
    return {"receipt": packet, "body": decode_body(packet["body"]), "admitted": False}

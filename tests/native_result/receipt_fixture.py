"""Deterministic native-review-admission/v1 fixture shared with A1 and Stargazer.

All IDs are fixed so another implementation can reproduce receipt_sha256 from
the canonical form: JSON, sorted keys, separators (",", ":"), ensure_ascii,
SHA256 over every receipt field except receipt_sha256, created_at and issuer_signature;
issuer_signature = Ed25519 over b"native-review-admission/v1\n" + receipt_sha256 (ascii hex).
"""
import base64
import copy
import hashlib
import json

BODY = b"VERDICT: SHIP\nhead=" + b"1" * 40 + b"\nall criteria PASS\n"


SEED = bytes([1]) * 32  # public test-only key; never an issuer credential


def _key(seed=SEED):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed)


def issuer_public(seed=SEED):
    from cryptography.hazmat.primitives import serialization
    raw = _key(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(value, seed=SEED):
    message = b"native-review-admission/v1\n" + value["receipt_sha256"].encode()
    value["issuer_signature"] = base64.b64encode(_key(seed).sign(message)).decode()
    return value


def u(n):
    return "00000000-0000-4000-8000-%012d" % n


def h(ch):
    return ch * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def native_binding():
    return {"producer": u(3), "task": u(10), "stage": "review-receipt",
            "package_sha256": h("5"), "test": True}


def receiver():
    return {"task": u(20), "attempt": u(21), "session": u(22), "generation": 1}


def registration(ledger=None):
    if ledger is not None:
        return ledger.register(native_binding(), receiver())["registration"]
    return hashlib.sha256(canonical({"binding": native_binding(), "receiver": receiver()})).hexdigest()


def pin():
    return {
        "schema": "native-review-admission/v1", "enrollment_id": u(1), "authority_id": u(2),
        "product": {"owning_task_id": u(30), "observation_id": "a" * 32, "session_id": "b" * 32,
                    "authority_sha256": h("c"), "root_disposition_sha256": h("d")},
        "issuer_actor_id": u(4), "publisher_actor_id": u(5), "transport_source_id": u(3),
        "review_task_id": u(11), "source_task_id": u(10), "workspace_id": u(12),
        "project_id": u(13), "owner_actor_id": u(14), "reviewer_family": "codex",
        "author_family": "claude",
        "scope": {"domain": "t12-successor-synthetic", "namespace": u(40), "test": True,
                  "criteria_sha256": h("e"), "context_sha256": h("f")},
        "pins": {"source_repo": "evc/evc-mesh-runner", "source_sha": "1" * 40,
                 "package_sha256": h("5"), "manifest_sha256": h("6"), "config_sha256": h("7")},
        "receiver": receiver(),
        "native": {"registration": registration(), "binding": native_binding()}}


def enrollment():
    return {"pin": pin(), "assignment": {"owner": u(50), "sequence": 1},
            "issuer_key": issuer_public()}


def packet(body=BODY, seed=SEED, **change):
    value = pin()
    value.update(receipt_id=u(60), result_author_actor_id=u(61), verdict="SHIP",
                 body={"encoding": "base64", "bytes": base64.b64encode(body).decode(),
                       "sha256": hashlib.sha256(body).hexdigest()})
    for key, new in change.items():
        value[key] = new
    value["receipt_sha256"] = ""
    value["created_at"] = "2026-11-03T17:45:00Z"
    unsigned = {k: v for k, v in value.items() if k not in ("receipt_sha256", "created_at", "issuer_signature")}
    value["receipt_sha256"] = hashlib.sha256(canonical(unsigned)).hexdigest()
    return sign(value, seed)


def resign(value, seed=SEED):
    value = copy.deepcopy(value)
    unsigned = {k: v for k, v in value.items() if k not in ("receipt_sha256", "created_at", "issuer_signature")}
    value["receipt_sha256"] = hashlib.sha256(canonical(unsigned)).hexdigest()
    return sign(value, seed)


if __name__ == "__main__":
    print(json.dumps({"enrollment": enrollment(), "receipt": packet()}, sort_keys=True, indent=1))

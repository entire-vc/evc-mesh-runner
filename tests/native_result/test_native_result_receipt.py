"""Enrolled reviewer-receipt source: real files, real receiver process, no ledger mocks."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import receipt_fixture as fx
from mesh_runner import native_result_receipt as rc
from mesh_runner.native_result_channel import (RemoteSink, Unavailable, bootstrap_receipt, install, load,
                                               receive, signed)
from mesh_runner.native_result_ledger import Ledger, Refused, identity


@pytest.fixture
def ch(tmp_path):
    ledger = Ledger.initialize(tmp_path / "ledger")
    enrolled = fx.enrollment()
    owner = enrolled["assignment"]["owner"]
    sender, endpoint = tmp_path / "sender", tmp_path / "endpoint"
    bootstrap_receipt(sender, enrolled, str(uuid4()), owner)
    regs = install(endpoint, ledger, sender)
    command = [sys.executable, "-m", "mesh_runner.native_result_channel",
               "receive", "--channel", str(endpoint)]
    sink = RemoteSink(sender, command)
    return {"ledger": ledger, "enrolled": enrolled, "endpoint": endpoint, "regs": regs,
            "sink": sink, "sender": sender}


def deliver(ch):
    packet = ch["ledger"].delivery(ch["regs"]["native"], fx.receiver())
    return packet


def test_golden_fixture_is_stable_for_other_implementations():
    golden = json.loads((Path(__file__).parent / "fixtures" / "review_receipt_v1.json").read_text())
    assert golden == {"enrollment": fx.enrollment(), "receipt": fx.packet()}
    assert golden["receipt"]["receipt_sha256"] == (
        "acc1ce61f2811b5e92ede2fc2eb37269dc354b442c021f04c63cc5abeca71594")
    assert fx.registration() == golden["enrollment"]["pin"]["native"]["registration"]


def test_full_body_reaches_ordinary_consumer_and_is_not_admission(ch):
    reply = ch["sink"].deliver_receipt(fx.packet())
    assert reply["duplicate"] is False
    delivered = deliver(ch)
    assert delivered["value"]["status"] == "result"
    assert delivered["value"]["owner"] == ch["enrolled"]["assignment"]["owner"]
    got = rc.load(ch["endpoint"], delivered, ch["enrolled"])
    assert got["body"] == fx.BODY
    assert hashlib.sha256(got["body"]).hexdigest() == got["receipt"]["body"]["sha256"]
    assert got["admitted"] is False
    # ACK moves the transport cursor only; it grants nothing and cannot be replayed as permission.
    ack = ch["ledger"].ack(ch["regs"]["native"], delivered["event"], fx.receiver())
    assert ack["duplicate"] is False
    assert ch["ledger"].delivery(ch["regs"]["native"], fx.receiver()) is None
    assert rc.load(ch["endpoint"], delivered, ch["enrolled"])["admitted"] is False


def test_do_not_ship_is_delivered_as_data_never_admitted(ch):
    ch["sink"].deliver_receipt(fx.resign({**fx.packet(), "verdict": "DO-NOT-SHIP"}))
    got = rc.load(ch["endpoint"], deliver(ch), ch["enrolled"])
    assert got["receipt"]["verdict"] == "DO-NOT-SHIP" and got["admitted"] is False


def test_duplicate_delivery_and_crash_before_link_repair(ch):
    packet = fx.packet()
    first = ch["sink"].deliver_receipt(packet)
    again = ch["sink"].deliver_receipt(packet)
    assert again["event"] == first["event"] and again["duplicate"] is True
    delivered = deliver(ch)
    link = Path(ch["endpoint"]) / "receipts" / (packet["receipt_sha256"] + ".link")
    link.unlink()  # crash between event commit and linkage
    with pytest.raises(FileNotFoundError):
        rc.load(ch["endpoint"], delivered, ch["enrolled"])
    ch["sink"].deliver_receipt(packet)  # exact retry repairs the same linkage
    assert rc.load(ch["endpoint"], delivered, ch["enrolled"])["body"] == fx.BODY


def test_second_different_receipt_for_same_enrollment_refused(ch):
    ch["sink"].deliver_receipt(fx.packet())
    other = fx.packet(body=b"another review")
    other["receipt_id"] = fx.u(99)
    with pytest.raises(Unavailable):  # receiver refused: no commit reply, cursor retained
        ch["sink"].deliver_receipt(fx.resign(other))
    delivered = deliver(ch)
    assert delivered["value"]["receipt_sha256"] == fx.packet()["receipt_sha256"]


@pytest.mark.parametrize("path,new", [
    ("enrollment_id", fx.u(77)), ("authority_id", fx.u(77)), ("issuer_actor_id", fx.u(77)),
    ("publisher_actor_id", fx.u(77)), ("transport_source_id", fx.u(77)),
    ("review_task_id", fx.u(77)), ("source_task_id", fx.u(77)), ("workspace_id", fx.u(77)),
    ("project_id", fx.u(77)), ("owner_actor_id", fx.u(77)),
    ("reviewer_family", "claude"), ("author_family", "codex"), ("schema", "native-review-admission/v2"),
    ("product.session_id", "e" * 32), ("product.observation_id", "e" * 32),
    ("product.authority_sha256", "0" * 64), ("product.root_disposition_sha256", "0" * 64),
    ("scope.namespace", fx.u(77)), ("scope.test", False), ("scope.domain", "product"),
    ("scope.criteria_sha256", "0" * 64), ("scope.context_sha256", "0" * 64),
    ("pins.source_repo", "evc/other"), ("pins.source_sha", "2" * 40),
    ("pins.package_sha256", "0" * 64), ("pins.manifest_sha256", "0" * 64),
    ("pins.config_sha256", "0" * 64),
    ("receiver.session", fx.u(77)), ("receiver.generation", 2), ("receiver.attempt", fx.u(77)),
    ("receiver.task", fx.u(77)), ("native.registration", "0" * 64),
    ("native.binding.stage", "other"), ("native.binding.task", fx.u(77)),
    ("native.binding.producer", fx.u(77)), ("native.binding.package_sha256", "0" * 64),
])
def test_any_changed_binding_fails_closed_even_if_resigned(ch, path, new):
    value = copy.deepcopy(fx.packet())
    node = value
    *head, last = path.split(".")
    for part in head:
        node = node[part]
    node[last] = new
    with pytest.raises(Refused):
        rc.admit_packet(fx.resign(value), ch["enrolled"])
    with pytest.raises(Refused):
        ch["sink"].deliver_receipt(fx.resign(value))
    assert ch["ledger"].delivery(ch["regs"]["native"], fx.receiver()) is None


OTHER_SEED = bytes([2]) * 32


def test_publisher_forged_receipt_with_pinned_issuer_id_is_refused(ch):
    # A publisher holding the channel key can build every field and the digest,
    # but cannot sign as the enrolled issuer.
    forged = fx.packet(body=b"VERDICT: SHIP (fabricated)", seed=OTHER_SEED)
    assert forged["issuer_actor_id"] == ch["enrolled"]["pin"]["issuer_actor_id"]
    with pytest.raises(Refused):
        rc.admit_packet(forged, ch["enrolled"])
    with pytest.raises(Refused):
        ch["sink"].deliver_receipt(forged)
    # Bypass the local pre-check: the separate receiver process must refuse too.
    body = {"version": 1, "channel": identity(ch["sink"].desc), "kind": "receipt", "value": forged}
    with pytest.raises(Refused):
        receive(ch["endpoint"], signed(body, ch["sink"].key))
    assert ch["ledger"].delivery(ch["regs"]["native"], fx.receiver()) is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("issuer_signature"),
    lambda p: p.update(issuer_signature=""),
    lambda p: p.update(issuer_signature=base64.b64encode(b"\x00" * 64).decode()),
    lambda p: p.update(issuer_signature=base64.b64encode(b"\x00" * 63).decode()),
    lambda p: p.update(body={**p["body"], "bytes": base64.b64encode(b"x").decode(),
                             "sha256": hashlib.sha256(b"x").hexdigest()}),
])
def test_missing_or_invalid_issuer_signature_refused(ch, mutate):
    value = fx.packet()
    mutate(value)
    with pytest.raises(Refused):
        rc.admit_packet(value, ch["enrolled"])


def test_enrollment_requires_valid_issuer_key():
    for key in ("", "AAAA", base64.b64encode(b"\x00" * 31).decode(), None):
        value = fx.enrollment()
        value["issuer_key"] = key
        with pytest.raises(Refused):
            rc.enrollment(value)
    value = fx.enrollment()
    value.pop("issuer_key")
    with pytest.raises(Refused):
        rc.enrollment(value)


def test_receiver_session_is_canonical_uuid_not_product_hex(ch):
    bad = fx.packet()
    bad["receiver"]["session"] = bad["product"]["session_id"]
    with pytest.raises(Refused):
        rc.validate(fx.resign(bad))


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("created_at"),
    lambda p: p.update(extra="x"),
    lambda p: p.update(verdict="PASS"),
    lambda p: p["body"].update(sha256="0" * 64),
    lambda p: p["body"].update(bytes=base64.b64encode(b"changed").decode()),
    lambda p: p["body"].update(bytes=""),
    lambda p: p["body"].update(bytes="not base64!"),
    lambda p: p["body"].pop("bytes"),
    lambda p: p.update(body={"encoding": "base64", "sha256": "0" * 64}),
    lambda p: p["body"].update(encoding="hex"),
    lambda p: p.update(receipt_sha256="0" * 64),
    lambda p: p.update(created_at="yesterday"),
    lambda p: p["scope"].update(test="true"),
    lambda p: p["receiver"].update(generation=True),
    lambda p: p["native"]["binding"].update(test=1),
])
def test_unsigned_or_altered_packets_are_refused(ch, mutate):
    value = fx.packet()
    mutate(value)
    with pytest.raises(Refused):
        rc.admit_packet(value, ch["enrolled"])


def test_digest_only_and_altered_bytes_after_resign_refused(ch):
    digest_only = fx.packet()
    digest_only["body"] = {"encoding": "base64", "bytes": "", "sha256": hashlib.sha256(fx.BODY).hexdigest()}
    with pytest.raises(Refused):
        rc.validate(fx.resign(digest_only))
    altered = fx.packet()
    altered["body"]["bytes"] = base64.b64encode(fx.BODY + b"x").decode()
    with pytest.raises(Refused):
        rc.validate(fx.resign(altered))


def test_oversize_packet_refused_not_truncated(ch):
    big = fx.packet(body=b"x" * (rc.MAX_PACKET))
    with pytest.raises(Refused):
        rc.validate(big)
    with pytest.raises(Refused):
        ch["sink"].deliver_receipt(big)
    assert ch["ledger"].delivery(ch["regs"]["native"], fx.receiver()) is None


def test_stored_bytes_tamper_detected_by_consumer(ch):
    packet = fx.packet()
    ch["sink"].deliver_receipt(packet)
    delivered = deliver(ch)
    path = Path(ch["endpoint"]) / "receipts" / (packet["receipt_sha256"] + ".json")
    tampered = copy.deepcopy(packet)
    tampered["body"]["bytes"] = base64.b64encode(b"forged").decode()
    path.write_bytes(fx.canonical(tampered))
    with pytest.raises(Refused):
        rc.load(ch["endpoint"], delivered, ch["enrolled"])


def test_linkage_must_match_persisted_event_and_receiver(ch):
    ch["sink"].deliver_receipt(fx.packet())
    delivered = deliver(ch)
    with pytest.raises(Refused):
        rc.load(ch["endpoint"], {**delivered, "event": "0" * 64}, ch["enrolled"])
    with pytest.raises(Refused):
        rc.load(ch["endpoint"], {**delivered, "receiver": {**fx.receiver(), "generation": 2}}, ch["enrolled"])


def test_enrollment_rejects_publisher_as_issuer_and_same_family(ch):
    for edit in (lambda e: e["pin"].update(publisher_actor_id=e["pin"]["issuer_actor_id"]),
                 lambda e: e["pin"].update(transport_source_id=e["pin"]["issuer_actor_id"]),
                 lambda e: e["pin"].update(author_family=e["pin"]["reviewer_family"]),
                 lambda e: e["pin"]["native"]["binding"].update(test=False),
                 lambda e: e["pin"]["native"]["binding"].update(producer=fx.u(77)),
                 lambda e: e["assignment"].update(sequence=2),
                 lambda e: e["pin"].update(unknown="x")):
        value = fx.enrollment()
        edit(value)
        with pytest.raises(Refused):
            rc.enrollment(value)


def test_authenticated_source_assignment_is_not_frame_owner(ch):
    ch["sink"].deliver_receipt(fx.packet())
    assert deliver(ch)["value"]["owner"] == fx.u(50)
    assert fx.u(50) not in json.dumps(fx.packet())


def test_wrong_channel_key_and_native_kind_refused(ch):
    packet = fx.packet()
    body = {"version": 1, "channel": identity(ch["sink"].desc), "kind": "receipt", "value": packet}
    forged = signed(body, b"k" * 32)
    with pytest.raises(Refused):
        receive(ch["endpoint"], forged)
    native = {"version": 1, "channel": identity(ch["sink"].desc), "kind": "native",
              "value": rc.native_event(packet, ch["enrolled"])}
    with pytest.raises(Refused):
        receive(ch["endpoint"], signed(native, ch["sink"].key))
    assert ch["ledger"].delivery(ch["regs"]["native"], fx.receiver()) is None


def test_copied_endpoint_for_other_receiver_refused(ch, tmp_path):
    other = Ledger.initialize(tmp_path / "other-ledger")
    with pytest.raises(Refused):
        # Same bootstrap, different receiver registration => enrollment mismatch.
        bad = copy.deepcopy(fx.enrollment())
        bad["pin"]["native"]["registration"] = "0" * 64
        bootstrap_receipt(tmp_path / "bad-sender", bad, str(uuid4()), bad["assignment"]["owner"])
        install(tmp_path / "bad-endpoint", other, tmp_path / "bad-sender")


def test_old_receiver_generation_gets_nothing(ch):
    ch["sink"].deliver_receipt(fx.packet())
    with pytest.raises(Refused):
        ch["ledger"].delivery(ch["regs"]["native"], {**fx.receiver(), "generation": 2})

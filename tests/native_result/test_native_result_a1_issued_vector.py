"""Retained A1-issued packet -> A2 controls.

The retained synthetic vector is pinned by raw SHA256 so a silent edit fails.
The signing key is not stored anywhere.
Synthetic CI proof only: not an installed delivery and not admission.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import receipt_fixture as fx
from mesh_runner import native_result_receipt as rc
from mesh_runner.native_result_ledger import Refused

VECTOR = Path(__file__).parent / "fixtures" / "native_review_a1_issued_vector_v1.json"
VECTOR_SHA256 = "cc6fb51d5520d1c62330bff921d6ec2ca3f7a0183b96f0be6888e0abd2df4492"
A1_BLOB = "152c27ca35b6efc79355d846c96a52690ecc5d75"  # Provenance only; not release admission.


@pytest.fixture(scope="module")
def vec():
    raw = VECTOR.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTOR_SHA256
    return json.loads(raw)


@pytest.fixture
def packet(vec):
    return json.loads(vec["packet_canonical"])


@pytest.fixture
def enrolled(vec):
    return vec["a2_enrollment"]


def test_pin_and_provenance(vec):
    assert len(VECTOR.read_bytes()) == 6493
    assert hashlib.sha256(vec["packet_canonical"].encode()).hexdigest() == vec["packet_canonical_sha256"]
    assert vec["vector"] == "native-review-a1-issued/v1"


def test_positive_admit_packet_and_canonical_bytes(vec, packet, enrolled):
    assert fx.canonical(packet).decode() == vec["packet_canonical"]
    assert rc.admit_packet(packet, enrolled) is not None
    assert rc.receipt_digest(packet) == packet["receipt_sha256"]
    body = base64.b64decode(packet["body"]["bytes"], validate=True)
    assert hashlib.sha256(body).hexdigest() == packet["body"]["sha256"]
    assert packet["product"]["authority_sha256"] == vec["authority_sha256"]
    assert packet["product"]["root_disposition_sha256"] == vec["root_disposition_sha256"]
    for key, digest in (("authority_original_bytes", "authority_sha256"),
                        ("root_disposition_original_bytes", "root_disposition_sha256")):
        assert hashlib.sha256(base64.b64decode(vec[key])).hexdigest() == vec[digest]


def test_positive_native_event_is_data_not_admission(packet, enrolled):
    event = rc.native_event(packet, enrolled)
    assert event["status"] == "result"
    assert event["owner"] == enrolled["assignment"]["owner"]
    assert event["receipt_sha256"] == packet["receipt_sha256"]
    assert "admitted" not in json.dumps(event) or event.get("admitted") is False


def _refused(packet, enrolled):
    with pytest.raises(Refused):
        rc.admit_packet(packet, enrolled)


def test_forged_signature_refused(packet, enrolled):
    forged = copy.deepcopy(packet)
    forged["issuer_signature"] = fx.sign({"receipt_sha256": packet["receipt_sha256"]}, bytes([2]) * 32)["issuer_signature"]
    _refused(forged, enrolled)


def test_missing_authority_id_refused(packet, enrolled):
    value = copy.deepcopy(packet)
    value.pop("authority_id")
    _refused(value, enrolled)
    _refused(fx.resign(value), enrolled)


def test_altered_body_stale_and_recomputed_hash_refused(packet, enrolled):
    value = copy.deepcopy(packet)
    value["body"]["bytes"] = base64.b64encode(b"altered").decode()
    _refused(value, enrolled)  # stale sha256 + stale signature
    value["body"]["sha256"] = hashlib.sha256(b"altered").hexdigest()
    _refused(value, enrolled)  # body hash recomputed, receipt hash/signature stale
    _refused(fx.resign(value), enrolled)  # re-signed with a non-issuer (harness) key


def test_rebound_authority_id_refused(packet, enrolled):
    value = copy.deepcopy(packet)
    value["authority_id"] = fx.u(77)
    _refused(value, enrolled)
    _refused(fx.resign(value), enrolled)


# Corrected A1 vector: native.registration is the ledger-derived value, so the
# full bootstrap->receiver->ledger->ordinary load->ACK path runs without xfail.
def test_issued_packet_through_bootstrap_receiver_ledger_ack(vec, packet, enrolled, tmp_path):
    from uuid import uuid4
    from mesh_runner.native_result_channel import RemoteSink, bootstrap_receipt, install
    from mesh_runner.native_result_ledger import Ledger
    ledger = Ledger.initialize(tmp_path / "ledger")
    sender, endpoint = tmp_path / "sender", tmp_path / "endpoint"
    bootstrap_receipt(sender, enrolled, str(uuid4()), enrolled["assignment"]["owner"])
    regs = install(endpoint, ledger, sender)
    sink = RemoteSink(sender, [sys.executable, "-m", "mesh_runner.native_result_channel",
                               "receive", "--channel", str(endpoint)])
    receiver = enrolled["pin"]["receiver"]
    assert sink.deliver_receipt(packet)["duplicate"] is False
    delivered = ledger.delivery(regs["native"], receiver)
    assert delivered["value"]["status"] == "result"
    got = rc.load(endpoint, delivered, enrolled)
    assert got["body"] == base64.b64decode(packet["body"]["bytes"])
    assert hashlib.sha256(got["body"]).hexdigest() == packet["body"]["sha256"]
    assert got["admitted"] is False
    assert ledger.ack(regs["native"], delivered["event"], receiver)["duplicate"] is False
    assert ledger.delivery(regs["native"], receiver) is None
    assert sink.deliver_receipt(packet)["duplicate"] is True


def test_vector_registration_equals_ledger_derivation(enrolled, tmp_path):
    from mesh_runner.native_result_ledger import Ledger
    ledger = Ledger.initialize(tmp_path / "ledger")
    pin = enrolled["pin"]
    derived = ledger.register(pin["native"]["binding"], pin["receiver"])["registration"]
    assert pin["native"]["registration"] == derived
    assert derived == "8e869b5237c0ada5f7298bd6862035abe8e4ef2a1268fe6d66499efdbd50d82e"


def test_wrong_registration_still_refused(packet, enrolled, tmp_path):
    from uuid import uuid4
    from mesh_runner.native_result_channel import bootstrap_receipt, install
    from mesh_runner.native_result_ledger import Ledger
    bad = copy.deepcopy(enrolled)
    bad["pin"]["native"]["registration"] = "a" * 64
    ledger = Ledger.initialize(tmp_path / "ledger")
    sender, endpoint = tmp_path / "sender", tmp_path / "endpoint"
    bootstrap_receipt(sender, bad, str(uuid4()), bad["assignment"]["owner"])
    with pytest.raises(Refused):
        install(endpoint, ledger, sender)

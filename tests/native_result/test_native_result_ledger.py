"""Real private files, durable commits and independent process restart controls.

No Mesh/Runner/network fixture is represented as a native or receiver ACK here.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ledger import Ledger, Refused


@pytest.fixture
def channel(tmp_path):
    source = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
              "package_sha256": "a" * 64, "test": True}
    target = {"task": source["task"], "attempt": str(uuid4()),
              "session": str(uuid4()), "generation": 3}
    ledger = Ledger.initialize(tmp_path / "mailbox")
    registration = ledger.register(source, target)["registration"]
    frame = {"binding": source, "sequence": 1, "status": "result",
             "receipt_sha256": "b" * 64, "owner": "Steward", "reason": ""}
    return ledger, registration, source, target, frame


def publish(ledger, frame):
    return ledger.publish(frame, frame["binding"]["producer"], frame["binding"]["task"])


def test_outbox_retry_restart_exact_receiver_ack_and_readback(channel):
    ledger, rid, source, target, frame = channel
    assert ledger.readback(rid)["cursor"] == 0
    eid = publish(ledger, frame)["event"]
    first = ledger.delivery(rid, target)
    assert first["event"] == eid and first["value"] == frame
    assert ledger.readback(rid)["cursor"] == 0
    # A fresh OS process reads/retries the durable outbox, not an in-memory copy.
    cmd = [sys.executable, "-c", (
        "import json,sys; from mesh_runner.native_result_ledger import Ledger; "
        "s=Ledger(sys.argv[1]); print(json.dumps(s.delivery(sys.argv[2],json.loads(sys.argv[3]))))"
    ), str(ledger.root), rid, json.dumps(target)]
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    retried = json.loads(subprocess.check_output(cmd, env=env, text=True))
    assert retried == first
    ledger = Ledger(ledger.root)
    assert ledger.ack(rid, eid, target) == {"event": eid, "cursor": 1, "duplicate": False}
    assert Ledger(ledger.root).ack(rid, eid, target)["duplicate"] is True
    assert Ledger(ledger.root).delivery(rid, target) is None
    assert Ledger(ledger.root).readback(rid)["cursor"] == 1


@pytest.mark.parametrize("field", ["task", "session", "attempt", "generation"])
def test_wrong_receiver_cannot_deliver_or_ack_and_does_not_consume(channel, field):
    ledger, rid, source, target, frame = channel
    eid = publish(ledger, frame)["event"]
    first = ledger.delivery(rid, target)
    wrong = dict(target, **{field: 4 if field == "generation" else str(uuid4())})
    before = (ledger.root / "state.json").read_bytes()
    with pytest.raises(Refused, match="receiver identity changed"):
        ledger.delivery(rid, wrong)
    with pytest.raises(Refused, match="ACK identity mismatch"):
        ledger.ack(rid, eid, wrong)
    assert (ledger.root / "state.json").read_bytes() == before
    assert ledger.delivery(rid, target) == first


@pytest.mark.parametrize("field", ["producer", "task"])
def test_author_name_is_insufficient_authentication(channel, field):
    ledger, rid, source, target, frame = channel
    auth = dict(source, **{field: str(uuid4())})
    before = (ledger.root / "state.json").read_bytes()
    with pytest.raises(Refused, match="authenticated producer/task mismatch"):
        ledger.publish(frame, auth["producer"], auth["task"])
    assert (ledger.root / "state.json").read_bytes() == before
    assert ledger.delivery(rid, target) is None


@pytest.mark.parametrize("field,value", [
    ("stage", "freeze"), ("package_sha256", "c" * 64), ("test", False),
])
def test_wrong_binding_and_test_production_crossover_refused(channel, field, value):
    ledger, rid, source, target, frame = channel
    frame["binding"] = dict(source, **{field: value})
    with pytest.raises(Refused, match="no exact registration"):
        publish(ledger, frame)
    assert ledger.delivery(rid, target) is None


def test_duplicate_and_stale_sequence_control(channel):
    ledger, rid, source, target, frame = channel
    frame["sequence"] = 2
    result = publish(ledger, frame)
    assert result["duplicate"] is False
    assert publish(Ledger(ledger.root), frame) == dict(result, duplicate=True)
    changed = dict(frame, receipt_sha256="c" * 64)
    with pytest.raises(Refused, match="stale or reused sequence"):
        publish(ledger, changed)
    changed["sequence"] = 1
    with pytest.raises(Refused, match="stale or reused sequence"):
        publish(ledger, changed)
    assert ledger.delivery(rid, target)["event"] == result["event"]


@pytest.mark.parametrize("status", ["failed", "dead", "unknown"])
def test_terminal_evidence_uses_same_durable_delivery(channel, status):
    ledger, rid, source, target, frame = channel
    frame.update(status=status, reason="Authenticated source adapter evidence")
    eid = publish(ledger, frame)["event"]
    delivery = Ledger(ledger.root).delivery(rid, target)
    assert delivery["value"]["status"] == status
    assert delivery["value"]["owner"] == "Steward"
    assert delivery["value"]["reason"]
    assert ledger.ack(rid, eid, target)["cursor"] == 1


def test_failure_without_reason_refused(channel):
    ledger, rid, source, target, frame = channel
    frame["status"] = "unknown"
    with pytest.raises(Refused, match="explicit reason"):
        publish(ledger, frame)
    assert ledger.delivery(rid, target) is None


def test_multiple_receivers_have_independent_cursor_and_order(channel):
    ledger, rid, source, target, frame = channel
    second = dict(target, task=str(uuid4()), attempt=str(uuid4()), session=str(uuid4()))
    rid2 = ledger.register(source, second)["registration"]
    eid1 = publish(ledger, frame)["event"]
    frame2 = dict(frame, sequence=2, receipt_sha256="d" * 64)
    eid2 = publish(ledger, frame2)["event"]
    with pytest.raises(Refused, match="not pending"):
        ledger.ack(rid, eid2, target)
    assert ledger.delivery(rid, target)["event"] == eid1
    assert ledger.delivery(rid2, second)["event"] == eid1
    ledger.ack(rid, eid1, target)
    assert ledger.delivery(rid, target)["event"] == eid2
    assert ledger.delivery(rid2, second)["event"] == eid1


def test_registration_is_idempotent_but_new_generation_is_new_registration(channel):
    ledger, rid, source, target, frame = channel
    assert Ledger(ledger.root).register(source, target)["registration"] == rid
    newer = dict(target, generation=4)
    assert ledger.register(source, newer)["registration"] != rid


def test_corrupt_or_missing_store_never_resets_cursor(channel):
    ledger, rid, source, target, frame = channel
    (ledger.root / "state.json").write_text("{broken")
    with pytest.raises(Refused, match="state corrupt"):
        ledger.readback(rid)
    (ledger.root / "state.json").unlink()
    with pytest.raises(FileNotFoundError):
        Ledger(ledger.root).register(source, target)
    with pytest.raises(FileExistsError):
        Ledger.initialize(ledger.root)


def test_state_symlink_refused(channel, tmp_path):
    ledger, rid, source, target, frame = channel
    saved = (ledger.root / "state.json").read_bytes()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(saved)
    (ledger.root / "state.json").unlink()
    (ledger.root / "state.json").symlink_to(elsewhere)
    with pytest.raises(OSError):
        ledger.readback(rid)
    assert elsewhere.read_bytes() == saved

from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ledger import Ledger, Refused
from mesh_runner.native_result_wait import arm, observe, wake_text


@pytest.fixture
def state(tmp_path):
    owner = str(uuid4())
    attempt = {"id": str(uuid4()), "task_id": str(uuid4()), "session_id": str(uuid4()),
               "checkout": {"generation": 3}, "state": "waiting", "mode": "work"}
    task = {"id": attempt["task_id"], "assignee_id": owner, "assignee_type": "agent",
            "checked_out_by": None, "checkout_session_id": attempt["id"], "checkout_generation": 3}
    binding = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
               "package_sha256": "a" * 64, "test": True}
    receiver = {"task": attempt["task_id"], "attempt": attempt["id"], "session": attempt["session_id"], "generation": 3}
    ledger = Ledger.initialize(tmp_path / "ledger")
    rid = ledger.register(binding, receiver)["registration"]
    event = {"binding": binding, "sequence": 1, "status": "unknown", "receipt_sha256": "b" * 64,
             "owner": "Riker", "reason": "control: original native producer unavailable"}
    return ledger, rid, attempt, task, owner, event


def test_actual_outbox_observation_is_not_ack(state):
    ledger, rid, attempt, task, owner, event = state
    assert observe(ledger, rid, attempt, task, owner) is None
    ledger.publish(event, event["binding"]["producer"], event["binding"]["task"])
    packet = observe(ledger, rid, attempt, task, owner)
    assert packet == observe(Ledger(ledger.root), rid, attempt, task, owner)
    assert ledger.readback(rid)["cursor"] == 0
    text = wake_text(packet)
    assert "test=true status=unknown" in text
    assert event["reason"] in text
    assert packet["event"] in text
    assert "not operational approval" in text


@pytest.mark.parametrize("field,value", [
    ("id", str(uuid4())), ("assignee_id", str(uuid4())), ("assignee_type", "user"),
    ("checked_out_by", str(uuid4())), ("checkout_session_id", str(uuid4())),
    ("checkout_generation", 4), ("checkout_generation", True),
    ("human_gate", True), ("start_after", "2099-01-01T00:00:00Z")])
def test_fresh_mesh_receiver_changes_never_prepare_or_consume(state, field, value):
    ledger, rid, attempt, task, owner, event = state
    ledger.publish(event, event["binding"]["producer"], event["binding"]["task"])
    before = (ledger.root / "state.json").read_bytes()
    with pytest.raises(Refused):
        observe(ledger, rid, attempt, dict(task, **{field: value}), owner)
    assert (ledger.root / "state.json").read_bytes() == before


@pytest.mark.parametrize("field,value", [("session_id", str(uuid4())), ("state", "running"), ("mode", "review")])
def test_actual_attempt_context_changes_are_refused(state, field, value):
    ledger, rid, attempt, task, owner, event = state
    ledger.publish(event, event["binding"]["producer"], event["binding"]["task"])
    before = (ledger.root / "state.json").read_bytes()
    with pytest.raises(Refused):
        observe(ledger, rid, dict(attempt, **{field: value}), task, owner)
    assert (ledger.root / "state.json").read_bytes() == before


def test_boolean_generation_is_not_checkout_one(state):
    ledger, rid, attempt, task, owner, event = state
    attempt["checkout"]["generation"] = 1
    task["checkout_generation"] = True
    with pytest.raises(Refused, match="checkout context"):
        observe(ledger, rid, attempt, task, owner)


def test_non_object_checkout_is_refused(state):
    ledger, rid, attempt, task, owner, event = state
    attempt["checkout"] = [3]
    with pytest.raises(Refused, match="checkout context"):
        observe(ledger, rid, attempt, task, owner)


def test_normal_wait_release_retains_observation_fence_without_lease(state):
    ledger, rid, attempt, task, owner, event = state
    active = dict(attempt, state="running", checkout={"generation": 3, "token": "TEST-lease"})
    leased = dict(task, checked_out_by=owner)
    captured = arm(ledger, rid, active, leased, owner)
    envelope = {"kind": "native", "target": rid, "native_receiver": captured}
    waiting = dict(attempt, checkout={}, wait=envelope)
    assert "token" not in captured
    assert observe(ledger, rid, waiting, task, owner) is None
    ledger.publish(event, event["binding"]["producer"], event["binding"]["task"])
    # JSON restart readback is the same supported post-release observation.
    import json
    packet = observe(Ledger(ledger.root), rid, json.loads(json.dumps(waiting)), task, owner)
    assert packet["receiver"] == captured and ledger.readback(rid)["cursor"] == 0
    # Crash after the durable fence and remote release, before WAIT state commit:
    # settlement can repeat safely with either the old local token or its clear.
    for checkout in ({}, active["checkout"]):
        assert arm(ledger, rid, dict(active, checkout=checkout, wait=envelope), task, owner) == captured
    for changed in (dict(task, checkout_generation=4), dict(task, checkout_session_id=str(uuid4())),
                    dict(task, checked_out_by=str(uuid4())), dict(task, human_gate=True)):
        with pytest.raises(Refused):
            observe(ledger, rid, waiting, changed, owner)
    with pytest.raises(Refused):
        observe(ledger, rid, dict(waiting, session_id=str(uuid4())), task, owner)


def test_wait_cannot_arm_after_release_or_for_other_registration(state):
    ledger, rid, attempt, task, owner, event = state
    with pytest.raises(Refused):
        arm(ledger, rid, dict(attempt, state="running", checkout={}), task, owner)
    other = dict(ledger.readback(rid)["receiver"], session=str(uuid4()))
    wrong = ledger.register(event["binding"], other)["registration"]
    with pytest.raises(Refused, match="another receiver"):
        arm(ledger, wrong, dict(attempt, state="running"), dict(task, checked_out_by=owner), owner)

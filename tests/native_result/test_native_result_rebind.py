"""Rebind fences use real ledgers; no controller/Store replacement here."""
import copy
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ledger import Ledger, Refused, identity
from mesh_runner.native_result_rebind import plan, validate


@pytest.fixture
def repair(tmp_path):
    owner, task, attempt, session = [str(uuid4()) for _ in range(4)]
    source = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
              "package_sha256": "a" * 64, "test": True}
    receiver = {"task": task, "attempt": attempt, "session": session, "generation": 1}
    ledger = Ledger.initialize(tmp_path / "ledger")
    old_rid = ledger.register(source, receiver)["registration"]
    frame = {"binding": source, "sequence": 1, "status": "result", "receipt_sha256": "b" * 64,
             "owner": owner, "reason": ""}
    ledger.publish(frame, source["producer"], source["task"])
    pending = ledger.delivery(old_rid, receiver)
    old_wait = {"kind": "native", "target": old_rid, "native_receiver": receiver,
                "native_packet": pending, "native_grant": {"not": "reusable"},
                "native_delivery_failed": {"owner": owner, "reason": "receiver died"}}
    state = {"id": attempt, "task_id": task, "session_id": session, "agent": "Steward",
             "state": "waiting", "mode": "work", "pid": 0, "checkout": {}, "wait": old_wait}
    card = {"id": task, "assignee_id": owner, "assignee_type": "agent", "checked_out_by": None,
            "checkout_session_id": attempt, "checkout_generation": 2}
    target = dict(receiver, generation=2)
    rid = ledger.register(source, target)["registration"]
    request = {"version": 1, "request_id": str(uuid4()), "agent": "Steward", "task": task,
               "attempt": attempt, "session": session, "owner": owner,
               "generation": identity(old_wait), "old_registration": old_rid,
               "registration": rid, "binding": source, "authority_sha256": "c" * 64,
               "reason": "explicit reviewed source/receiver repair", "created": 1000, "expires": 1300}
    return locals()


def test_repair_rebinds_fresh_epoch_preserving_pending_and_old_ack_cursor(repair):
    t = repair
    before = copy.deepcopy(t["state"])
    wait, audit = plan(t["ledger"], t["request"], t["state"], t["card"], t["owner"])
    assert wait == {"kind": "native", "target": t["rid"], "native_receiver": t["target"],
                    "native_rebind": t["request"]["request_id"]}
    assert t["state"] == before
    assert audit["previous_wait"] == t["old_wait"]
    assert audit["pending_events_retained"] is True
    assert t["ledger"].delivery(t["old_rid"], t["receiver"]) == t["pending"]
    assert t["ledger"].readback(t["old_rid"])["cursor"] == 0
    assert t["ledger"].delivery(t["rid"], t["target"])["event"] == t["pending"]["event"]


@pytest.mark.parametrize("changes", [{"human_gate": True}, {"start_after": "2099-01-01"},
    {"assignee_id": str(uuid4())}, {"assignee_type": "user"}, {"checked_out_by": str(uuid4())},
    {"checkout_session_id": str(uuid4())}, {"checkout_generation": 3}, {"checkout_generation": True}])
def test_current_access_revocation_and_epoch_change_refuse(repair, changes):
    t = repair
    with pytest.raises(Refused):
        plan(t["ledger"], t["request"], t["state"], dict(t["card"], **changes), t["owner"])
    assert t["ledger"].readback(t["old_rid"])["cursor"] == 0


@pytest.mark.parametrize("changes", [{"state": "running"}, {"state": "ready"}, {"pid": 123},
    {"checkout": {"generation": 2}}, {"session_id": str(uuid4())}, {"mode": "review"},
    {"id": str(uuid4())}, {"task_id": str(uuid4())}, {"agent": "other"}])
def test_active_or_replaced_receiver_refuses(repair, changes):
    t = repair
    with pytest.raises(Refused):
        plan(t["ledger"], t["request"], dict(t["state"], **changes), t["card"], t["owner"])


@pytest.mark.parametrize("field", ["generation", "old_registration", "registration", "owner", "binding"])
def test_exact_request_binding_and_old_wait_cas_fence(repair, field):
    t = repair
    request = copy.deepcopy(t["request"])
    request[field] = (dict(request["binding"], stage="actual028") if field == "binding" else
                      str(uuid4()) if field == "owner" else "f" * 64)
    with pytest.raises(Refused):
        plan(t["ledger"], request, t["state"], t["card"], t["owner"])


def test_request_for_old_wait_cannot_clear_a_new_hold(repair):
    t = repair
    changed = copy.deepcopy(t["state"])
    changed["wait"]["native_delivery_failed"]["reason"] = "new failure evidence"
    with pytest.raises(Refused):
        plan(t["ledger"], t["request"], changed, t["card"], t["owner"])


def test_unknown_registration_failure_requires_explicit_hold_and_inactive_process(repair):
    t = repair
    state = dict(t["state"], state="unknown")
    assert plan(t["ledger"], t["request"], state, t["card"], t["owner"])[0]["target"] == t["rid"]
    state["wait"] = dict(t["old_wait"])
    state["wait"].pop("native_delivery_failed")
    request = dict(t["request"], generation=identity(state["wait"]))
    with pytest.raises(Refused):
        plan(t["ledger"], request, state, t["card"], t["owner"])


def test_next_stage_uses_new_registration_without_acknowledging_previous_stage(repair):
    t = repair
    source = dict(t["source"], stage="actual028", package_sha256="d" * 64)
    rid = t["ledger"].register(source, t["target"])["registration"]
    request = dict(t["request"], registration=rid, binding=source)
    assert plan(t["ledger"], request, t["state"], t["card"], t["owner"])[0]["target"] == rid
    assert t["ledger"].delivery(t["old_rid"], t["receiver"]) == t["pending"]
    assert t["ledger"].readback(rid)["cursor"] == 0


@pytest.mark.parametrize("changes", [{"version": True}, {"authority_sha256": "bad"}, {"reason": ""},
    {"reason": "x" * 513}, {"created": True}, {"expires": 2000}, {"expires": 1000}, {"extra": 1}])
def test_malformed_or_unbounded_repair_request_refuses(repair, changes):
    with pytest.raises(Refused):
        validate(dict(repair["request"], **changes), "Steward")


def test_same_registration_is_not_a_repair(repair):
    with pytest.raises(Refused):
        validate(dict(repair["request"], registration=repair["old_rid"]), "Steward")


def test_production_rebind_cannot_skip_enrolled_authenticated_channel(repair):
    t = repair
    source = dict(t["source"], test=False)
    rid = t["ledger"].register(source, t["target"])["registration"]
    request = dict(t["request"], registration=rid, binding=source)
    with pytest.raises(Refused, match="enrolled channel"):
        plan(t["ledger"], request, t["state"], t["card"], t["owner"])

"""File-only transport controls; no controller/SQLite or original native ACK."""
from copy import deepcopy
import hashlib
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
from mesh_runner.native_result_resume import ack_frame, authorize, authorize_codex, consume, instruction, prepare_prompt
from mesh_runner.native_result_source import ACK_MARKER, MARKER


def append(source, frame, role="assistant", marker=ACK_MARKER):
    with source.open("ab") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role, "content": [{
                "type": "output_text", "text": marker + json.dumps(frame)}]}}).encode() + b"\n")


@pytest.fixture
def delivery(tmp_path):
    owner = str(uuid4())
    target = {"task": str(uuid4()), "attempt": str(uuid4()), "session": str(uuid4()), "generation": 3}
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    ledger = Ledger.initialize(tmp_path / "ledger")
    rid = ledger.register(contract, target)["registration"]
    ledger.publish({"binding": contract, "sequence": 1, "status": "result",
                    "receipt_sha256": "b" * 64, "owner": "TEST", "reason": ""},
                   contract["producer"], contract["task"])
    packet = ledger.delivery(rid, target)
    root = tmp_path / "codex" / "sessions"
    root.mkdir(parents=True)
    source = root / ("rollout-TEST-" + target["session"] + ".jsonl")
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": target["session"]}}) + "\n")
    attempt = {"id": target["attempt"], "task_id": target["task"], "session_id": target["session"],
               "checkout": {"generation": 4}, "mode": "work", "state": "ready"}
    task = {"id": target["task"], "assignee_id": owner, "assignee_type": "agent",
            "checked_out_by": owner, "checkout_generation": 4, "checkout_session_id": target["attempt"]}
    return ledger, packet, attempt, task, owner, root, source


def grant_for(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    return authorize(ledger, packet, attempt, task, owner, root, source)


def running(attempt):
    return dict(attempt, state="running")


def unsent(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    env = {"CODEX_HOME": str(root.parent)}
    current = dict(attempt, wait={"native_packet": packet})
    prompt, grant = prepare_prompt(ledger, current, task, owner, env, "TEST delivery")
    current["wait"].update(native_grant=grant, interactive_prompt_intent={
        "retryable": True, "prompt": prompt, "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "harness": "codex", "resume": True, "session_id": attempt["session_id"]})
    return current, env, prompt, grant


def test_unsent_restart_keeps_fence_and_consumes_only_actual_ack(delivery, tmp_path):
    ledger, packet, attempt, task, owner, root, source = delivery
    current, env, prompt, grant = unsent(delivery)
    # A new checkout epoch is normal after releasing an unsent launch. Native
    # source may grow between attempts; resetting its fence would lose evidence.
    source.write_text(source.read_text() + json.dumps({"type": "event_msg", "payload": {}}) + "\n")
    current["checkout"]["generation"] = task["checkout_generation"] = 5
    state = tmp_path / "unsent-TEST.json"
    state.write_text(json.dumps({"ledger": str(ledger.root), "attempt": current, "task": task,
                               "owner": owner, "env": env, "prompt": prompt}))
    code = '''import json,sys
from mesh_runner.native_result_ledger import Ledger
from mesh_runner.native_result_resume import prepare_prompt
d=json.load(open(sys.argv[1]))
p,g=prepare_prompt(Ledger(d["ledger"]),d["attempt"],d["task"],d["owner"],d["env"],d["prompt"],complete=True)
print(json.dumps({"prompt":p,"grant":g}))'''
    child = subprocess.run([sys.executable, "-c", code, str(state)], cwd=SCRIPTS,
                           capture_output=True, text=True, check=True)
    retried = json.loads(child.stdout)
    assert retried["prompt"] == prompt
    assert retried["grant"]["source"] == grant["source"]
    assert retried["grant"]["receiver"]["generation"] == 5
    assert ledger.readback(packet["registration"])["cursor"] == 0
    assert consume(ledger, retried["grant"], running(current), task, owner)["ack"] is None
    append(source, grant["ack"])
    assert consume(ledger, retried["grant"], running(current), task, owner)["ack"]["cursor"] == 1


@pytest.mark.parametrize("field,value", [
    ("retryable", False), ("retryable", 1), ("prompt", "substituted"),
    ("sha256", "0" * 64), ("harness", "claude"), ("resume", False),
    ("session_id", str(uuid4()))])
def test_retry_requires_original_controller_unsent_intent(delivery, field, value):
    ledger, packet, attempt, task, owner, root, source = delivery
    current, env, prompt, grant = unsent(delivery)
    current["wait"]["interactive_prompt_intent"][field] = value
    with pytest.raises(Refused):
        prepare_prompt(ledger, current, task, owner, env, prompt, complete=True)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_retry_revocation_or_replaced_source_preserves_pending(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    current, env, prompt, grant = unsent(delivery)
    with pytest.raises(Refused):
        prepare_prompt(ledger, current, dict(task, checked_out_by=None), owner, env, prompt, complete=True)
    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
    with pytest.raises(Refused, match="replaced"):
        prepare_prompt(ledger, current, task, owner, env, prompt, complete=True)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_existing_grant_cannot_be_reauthorized_as_new_prompt(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    current, env, prompt, grant = unsent(delivery)
    with pytest.raises(Refused, match="exact unsent"):
        prepare_prompt(ledger, current, task, owner, env, prompt)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_new_checkout_handoff_waits_for_actual_receiver_output(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    assert grant["receiver"]["generation"] == 4 and packet["receiver"]["generation"] == 3
    assert consume(ledger, grant, running(attempt), task, owner)["ack"] is None
    append(source, grant["ack"], role="user")
    append(source, grant["ack"], role="tool")
    append(source, grant["ack"], marker=MARKER)
    assert consume(ledger, grant, running(attempt), task, owner)["ack"] is None
    assert ledger.readback(packet["registration"])["cursor"] == 0
    append(source, grant["ack"])
    result = consume(ledger, grant, running(attempt), task, owner)
    assert result["ack"] == {"event": packet["event"], "cursor": 1, "duplicate": False}
    assert consume(ledger, result["grant"], running(attempt), task, owner)["ack"] is None
    assert "test" in instruction(grant) and ACK_MARKER in instruction(grant)


def test_old_ack_before_spawn_is_excluded(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    append(source, ack_frame(packet, owner))
    grant = grant_for(delivery)
    assert consume(ledger, grant, running(attempt), task, owner)["ack"] is None
    assert ledger.readback(packet["registration"])["cursor"] == 0


@pytest.mark.parametrize("field,value", [
    ("id", str(uuid4())), ("assignee_id", str(uuid4())), ("assignee_type", "user"),
    ("checked_out_by", None), ("checked_out_by", str(uuid4())),
    ("checkout_session_id", str(uuid4())), ("checkout_generation", 5),
    ("checkout_generation", True), ("human_gate", True), ("start_after", "2099-01-01")])
def test_current_access_revocations_refuse_before_ack(delivery, field, value):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    append(source, grant["ack"])
    before = (ledger.root / "state.json").read_bytes()
    with pytest.raises(Refused):
        consume(ledger, grant, running(attempt), dict(task, **{field: value}), owner)
    assert (ledger.root / "state.json").read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("session_id", str(uuid4())), ("id", str(uuid4())), ("task_id", str(uuid4())),
    ("state", "waiting"), ("mode", "review"), ("checkout", {"generation": 3})])
def test_wrong_attempt_never_authorizes_resume(delivery, field, value):
    ledger, packet, attempt, task, owner, root, source = delivery
    with pytest.raises(Refused):
        authorize(ledger, packet, dict(attempt, **{field: value}), task, owner, root, source)
    assert ledger.readback(packet["registration"])["cursor"] == 0


@pytest.mark.parametrize("mutation", ["event", "test", "generation", "ack", "marker"])
def test_changed_packet_or_authorization_rejected(delivery, mutation):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    append(source, grant["ack"])
    if mutation == "event":
        grant["packet"]["event"] = "c" * 64
    elif mutation == "test":
        grant["packet"]["value"]["binding"]["test"] = False
    elif mutation == "generation":
        grant["receiver"]["generation"] = 3
    elif mutation == "ack":
        grant["ack"]["reason"] = "not-consumed"
    else:
        grant["source"]["marker"] = MARKER
    with pytest.raises(Refused):
        consume(ledger, grant, running(attempt), task, owner)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_wrong_ack_after_resume_does_not_consume(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    wrong = deepcopy(grant["ack"])
    wrong["receipt_sha256"] = "c" * 64
    append(source, wrong)
    with pytest.raises(Refused, match="contents mismatch"):
        consume(ledger, grant, running(attempt), task, owner)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_original_receiver_source_replaced_after_resume(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    source.rename(source.with_suffix(".old"))
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": packet["receiver"]["session"]}}) + "\n")
    append(source, grant["ack"])
    with pytest.raises(Refused, match="replaced"):
        consume(ledger, grant, running(attempt), task, owner)
    assert ledger.readback(packet["registration"])["cursor"] == 0


def test_launch_environment_has_exact_one_receiver_rollout(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = authorize_codex(ledger, packet, attempt, task, owner, {"CODEX_HOME": str(root.parent)})
    assert grant["source"]["source"] == str(source)
    duplicate = root / ("rollout-copy-" + attempt["session_id"] + ".jsonl")
    duplicate.write_bytes(source.read_bytes())
    with pytest.raises(Refused, match="one original"):
        authorize_codex(ledger, packet, attempt, task, owner, {"CODEX_HOME": str(root.parent)})


def test_partial_record_cannot_be_resume_fence(delivery):
    ledger, packet, attempt, task, owner, root, source = delivery
    with source.open("ab") as stream:
        stream.write(b'{"partial":')
    with pytest.raises(Refused, match="complete record"):
        grant_for(delivery)


def test_restart_after_ack_before_controller_cursor_commit(delivery, tmp_path):
    ledger, packet, attempt, task, owner, root, source = delivery
    grant = grant_for(delivery)
    append(source, grant["ack"])
    context = tmp_path / "TEST-grant.json"
    context.write_text(json.dumps({"grant": grant, "attempt": running(attempt), "task": task, "owner": owner}))
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    # Actual worker dies after durable ACK; the next OS process replays the
    # original persisted grant, proving the source cursor was not assumed saved.
    code = '''import json,os,sys
from mesh_runner.native_result_ledger import Ledger
from mesh_runner.native_result_resume import consume
ctx=json.load(open(sys.argv[2]))
result=consume(Ledger(sys.argv[1]),ctx['grant'],ctx['attempt'],ctx['task'],ctx['owner'])
assert result['ack']['duplicate'] is False
os._exit(23)
'''
    first = subprocess.run([sys.executable, "-c", code, str(ledger.root), str(context)], env=env, capture_output=True)
    assert first.returncode == 23, first.stderr.decode()
    code = code[:code.index("assert result")] + "print(json.dumps(result['ack']))\n"
    second = subprocess.run([sys.executable, "-c", code, str(ledger.root), str(context)], env=env,
                            check=True, capture_output=True, text=True)
    assert json.loads(second.stdout) == {"event": packet["event"], "cursor": 1, "duplicate": True}

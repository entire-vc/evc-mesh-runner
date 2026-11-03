"""Explicit TEST transport control across real OS processes, not a native ACK.

No controller, Mesh lease, SQLite, operational receipt or runtime is involved.
The source and receiver contexts below are local test fixtures.
"""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger


def run(code, *args, expected=0):
    result = subprocess.run([sys.executable, "-c", code, *map(str, args)],
                            cwd=SCRIPTS, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    return result.stdout


def test_transport_control_source_to_receiver_consumption_ack_crash_and_retry(tmp_path):
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    attempt = {"id": str(uuid4()), "task_id": str(uuid4()), "session_id": str(uuid4()),
               "checkout": {"generation": 1}, "state": "waiting", "mode": "work"}
    owner = str(uuid4())
    task = {"id": attempt["task_id"], "assignee_id": owner, "assignee_type": "agent",
            "checkout_session_id": attempt["id"], "checkout_generation": 1}
    receiver = {"task": attempt["task_id"], "attempt": attempt["id"],
                "session": attempt["session_id"], "generation": 1}
    native = tmp_path / "test-sessions"
    native.mkdir()
    source = native / "test-rollout.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": contract["producer"]}}) + "\n")
    journal = SourceJournal.initialize(tmp_path / "source-cursor", native, source, contract)
    ledger = Ledger.initialize(tmp_path / "outbox")
    rid = ledger.register(contract, receiver)["registration"]
    frame = {"binding": contract, "sequence": 1, "status": "result", "receipt_sha256": "b" * 64,
             "owner": "Riker", "reason": "TEST transport control; not actual023"}
    run('''import json,os,sys
from mesh_runner.native_result_source import MARKER
frame=json.loads(sys.argv[2])
with open(sys.argv[1], "a") as stream:
    stream.write(json.dumps({"type":"response_item", "payload":{"type":"message", "role":"assistant",
        "content":[{"type":"output_text", "text":MARKER+json.dumps(frame)}]}})+"\\n")
    stream.flush(); os.fsync(stream.fileno())
''', source, json.dumps(frame))
    ingest = '''import json,sys
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger
print(json.dumps(SourceJournal(sys.argv[1]).ingest(Ledger(sys.argv[2]))))
'''
    published = json.loads(run(ingest, journal.root, ledger.root))
    assert len(published["events"]) == 1 and not published["events"][0]["duplicate"]
    # New observer process after source has exited: no second event/cursor loss.
    assert json.loads(run(ingest, journal.root, ledger.root))["events"] == []
    context = tmp_path / "test-context.json"
    context.write_text(json.dumps({"attempt": attempt, "task": task, "owner": owner, "rid": rid}))
    observed = json.loads(run('''import json,sys
from mesh_runner.native_result_ledger import Ledger
from mesh_runner.native_result_wait import observe
ctx=json.load(open(sys.argv[2]))
print(json.dumps(observe(Ledger(sys.argv[1]), ctx["rid"], ctx["attempt"], ctx["task"], ctx["owner"])))
''', ledger.root, context))
    assert observed["event"] == published["events"][0]["event"]
    assert ledger.readback(rid)["cursor"] == 0
    consumed = tmp_path / "TEST-consumed.json"
    # The receiver verifies full content, persists consumption, ACKs, then dies
    # before responding. This is an actual process exit, not an internal mock.
    run('''import json,os,sys
from mesh_runner.native_result_ledger import Ledger, canonical
from mesh_runner.native_result_wait import observe
ctx=json.load(open(sys.argv[2])); ledger=Ledger(sys.argv[1])
packet=observe(ledger,ctx["rid"],ctx["attempt"],ctx["task"],ctx["owner"])
assert packet["value"]==json.loads(sys.argv[4])
assert packet["value"]["binding"]["test"] is True
with open(sys.argv[3],"wb") as stream:
    stream.write(canonical(packet)); stream.flush(); os.fsync(stream.fileno())
ledger.ack(ctx["rid"],packet["event"],packet["receiver"])
os._exit(23)
''', ledger.root, context, consumed, json.dumps(frame), expected=23)
    consumed_packet = json.loads(consumed.read_text())
    assert consumed_packet == observed
    assert Ledger(ledger.root).readback(rid)["cursor"] == 1
    retried = json.loads(run('''import json,sys
from mesh_runner.native_result_ledger import Ledger
packet=json.load(open(sys.argv[2]))
print(json.dumps(Ledger(sys.argv[1]).ack(packet["registration"],packet["event"],packet["receiver"])))
''', ledger.root, consumed))
    assert retried == {"event": observed["event"], "cursor": 1, "duplicate": True}
    assert Ledger(ledger.root).delivery(rid, receiver) is None

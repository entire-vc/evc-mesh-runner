"""Real old/new rollout files and a separate authenticated receiver process."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_channel import RemoteSink, bootstrap, descriptor, install, load
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger, Refused, identity
from mesh_runner.native_result_lineage import validate
from mesh_runner.native_result_source import MARKER


def append(path, frame):
    row = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": MARKER + json.dumps(frame)}]}}
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


@pytest.fixture
def transition(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    old, new = root / "old.jsonl", root / "new.jsonl"
    original, successor, owner = str(uuid4()), str(uuid4()), str(uuid4())
    for file, sid in ((old, original), (new, successor)):
        file.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
    contract = {"producer": original, "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    previous = SourceJournal.initialize(tmp_path / "old-cursor", root, old, contract)
    ledger = Ledger.initialize(tmp_path / "ledger")
    target = {"task": str(uuid4()), "attempt": str(uuid4()), "session": str(uuid4()), "generation": 1}
    oldrid = ledger.register(contract, target)["registration"]
    oldframe = {"binding": contract, "sequence": 1, "status": "result",
                "receipt_sha256": "b" * 64, "owner": owner, "reason": ""}
    append(old, oldframe)
    previous.ingest(ledger)
    frame = dict(oldframe, binding=dict(contract, producer=successor))
    # This pre-enrollment frame must never be replayed as a successor delivery.
    append(new, frame)
    return locals()


def enroll_next(t, directory="new-cursor"):
    return SourceJournal.initialize_successor(t["tmp_path"] / directory, t["previous"],
        t["root"], t["new"], t["successor"], t["owner"], "c" * 64)


def test_explicit_succession_preserves_old_pending_and_fences_new_history(transition):
    t = transition
    oldstate = t["previous"].enrollment()
    oldpacket = t["ledger"].delivery(t["oldrid"], t["target"])
    following = enroll_next(t)
    state = following.enrollment()
    assert state["version"] == 2
    assert state["lineage"]["previous"] == oldstate["source"]
    assert state["source"]["cursor"] == t["new"].stat().st_size
    sender, endpoint = t["tmp_path"] / "sender", t["tmp_path"] / "endpoint"
    bootstrap(sender, following, t["target"], str(uuid4()), t["owner"])
    desc, _ = load(sender)
    assert desc["version"] == 2 and desc["lineage"] == state["lineage"]
    registration = install(endpoint, t["ledger"], sender)
    rid = registration["native"]
    assert rid != t["oldrid"] and t["ledger"].delivery(rid, t["target"]) is None
    command = [sys.executable, "-m", "mesh_runner.native_result_channel",
               "receive", "--channel", str(endpoint)]
    assert following.ingest(RemoteSink(sender, command))["events"] == []
    append(t["new"], t["frame"])
    # Restart the source-side sender process, loading only committed disk state.
    script = ("import sys; sys.path.insert(0,sys.argv[1]); "
              "from mesh_runner.native_result_ingest import SourceJournal; "
              "from mesh_runner.native_result_channel import RemoteSink; "
              "SourceJournal(sys.argv[2]).ingest(RemoteSink(sys.argv[3],sys.argv[4:]))")
    subprocess.run([sys.executable, "-c", script, str(SCRIPTS), str(following.root),
                    str(sender), *command], check=True, capture_output=True, timeout=10)
    packet = t["ledger"].delivery(rid, t["target"])
    assert packet["value"] == t["frame"]
    assert packet["value"]["binding"]["producer"] == t["successor"]
    assert t["previous"].enrollment() == oldstate
    assert t["ledger"].delivery(t["oldrid"], t["target"]) == oldpacket
    assert t["ledger"].readback(t["oldrid"])["cursor"] == 0
    assert t["ledger"].ack(rid, identity(t["frame"]), t["target"])["cursor"] == 1
    assert t["ledger"].readback(t["oldrid"])["cursor"] == 0


def test_undrained_old_input_cannot_be_abandoned(transition):
    t = transition
    append(t["old"], dict(t["oldframe"], sequence=2))
    before = t["previous"].enrollment()
    with pytest.raises(Refused, match="drained"):
        enroll_next(t)
    assert t["previous"].enrollment() == before
    assert not (t["tmp_path"] / "new-cursor").exists()


@pytest.mark.parametrize("mutation", ["task", "stage", "package_sha256", "test", "producer", "fence", "inode", "authority"])
def test_changed_lineage_context_is_refused(transition, mutation):
    state = enroll_next(transition).enrollment()
    lineage, source = copy.deepcopy(state["lineage"]), state["source"]
    replacements = {"task": str(uuid4()), "stage": "freeze", "package_sha256": "d" * 64,
                    "test": False, "producer": lineage["previous"]["binding"]["producer"]}
    if mutation in replacements:
        lineage["successor"]["binding"][mutation] = replacements[mutation]
    elif mutation == "fence":
        lineage["successor"]["cursor"] += 1
    elif mutation == "inode":
        lineage["previous"]["inode"] = lineage["successor"]["inode"]
    else:
        lineage["authority_sha256"] = "not-a-digest"
    with pytest.raises(Refused):
        validate(lineage, source)


@pytest.mark.parametrize("change", ["append-old", "replace-old", "replace-new", "wrong-session", "partial-new"])
def test_local_reenrollment_checks_real_files_before_bootstrap(transition, change):
    t = transition
    if change == "wrong-session":
        t["new"].write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid4())}}) + "\n")
        with pytest.raises(Refused, match="producer mismatch"):
            enroll_next(t)
        return
    if change == "partial-new":
        with t["new"].open("a") as stream:
            stream.write('{"type":')
        with pytest.raises(Refused, match="complete record"):
            enroll_next(t)
        return
    following = enroll_next(t)
    if change == "append-old":
        append(t["old"], dict(t["oldframe"], sequence=2))
    else:
        path = t["old"] if change == "replace-old" else t["new"]
        path.rename(path.with_suffix(".retired"))
        path.write_bytes(path.with_suffix(".retired").read_bytes())
    with pytest.raises(Refused, match="changed"):
        bootstrap(t["tmp_path"] / "sender", following, t["target"], str(uuid4()), t["owner"])
    assert not (t["tmp_path"] / "sender").exists()


def test_owner_and_observer_cannot_impersonate_lineage(transition):
    t = transition
    following = enroll_next(t)
    with pytest.raises(Refused, match="operator differs"):
        bootstrap(t["tmp_path"] / "bad-owner", following, t["target"], str(uuid4()), str(uuid4()))
    bootstrap(t["tmp_path"] / "sender", following, t["target"], str(uuid4()), t["owner"])
    desc, _ = load(t["tmp_path"] / "sender")
    desc["observer"] = t["original"]
    with pytest.raises(Refused, match="distinct"):
        descriptor(desc)

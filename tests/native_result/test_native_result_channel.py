"""Real files and separate receiver processes; no transport/ledger mocks."""
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
from mesh_runner.native_result_channel import (RemoteSink, Unavailable, bootstrap, failure_binding,
                                   descriptor, install, load, receive, signed)
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger, Refused, identity
from mesh_runner.native_result_source import MARKER
from mesh_runner.native_result_wait import observe
from mesh_runner.native_result_process import snapshot


@pytest.fixture
def channel(tmp_path):
    native = tmp_path / "sessions"
    native.mkdir()
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    source = native / "native.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": contract["producer"]}}) + "\n")
    journal = SourceJournal.initialize(tmp_path / "cursor", native, source, contract)
    ledger = Ledger.initialize(tmp_path / "ledger")
    target = {"task": str(uuid4()), "attempt": str(uuid4()), "session": str(uuid4()), "generation": 1}
    sender, endpoint = tmp_path / "sender", tmp_path / "endpoint"
    bootstrap(sender, journal, target, str(uuid4()), str(uuid4()))
    registrations = install(endpoint, ledger, sender)
    command = [sys.executable, "-m", "mesh_runner.native_result_channel",
               "receive", "--channel", str(endpoint)]
    frame = {"binding": contract, "sequence": 1, "status": "result", "receipt_sha256": "b" * 64,
             "owner": "Steward", "reason": ""}
    return {"source": source, "journal": journal, "ledger": ledger, "target": target,
            "sender": sender, "endpoint": endpoint, "registrations": registrations,
            "command": command, "frame": frame}


def append(channel, frame=None):
    frame = channel["frame"] if frame is None else frame
    row = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": MARKER + json.dumps(frame)}]}}
    with channel["source"].open("a") as stream:
        stream.write(json.dumps(row) + "\n")


@pytest.mark.parametrize("change", ["source", "operator", "authority", "birth", "observer"])
def test_pinned_process_descriptor_rejects_changed_scope(channel, change):
    sender = channel["sender"].parent / "pinned"
    bootstrap(sender, channel["journal"], channel["target"], str(uuid4()), str(uuid4()),
              process=snapshot(os.getpid()), authority_sha256="d" * 64)
    desc, _ = load(sender)
    assert desc["version"] == 3
    if change == "source":
        desc["source"]["binding"]["package_sha256"] = "e" * 64
    elif change == "operator":
        desc["process"]["operator"] = str(uuid4())
    elif change == "authority":
        desc["process"]["authority_sha256"] = "invalid"
    elif change == "birth":
        desc["process"]["identity"]["birth"] = {}
    else:
        desc["observer"] = desc["source"]["binding"]["producer"]
    with pytest.raises(Refused):
        descriptor(desc)


def test_legacy_unpinned_channel_cannot_be_promoted_to_production(channel):
    desc, _ = load(channel["sender"])
    desc["source"]["binding"]["test"] = False
    with pytest.raises(Refused, match="process fence"):
        descriptor(desc)


def test_separate_receiver_commit_precedes_cursor_and_consumption(channel):
    append(channel)
    sink = RemoteSink(channel["sender"], channel["command"])
    result = channel["journal"].ingest(sink)
    eid = identity(channel["frame"])
    assert result["events"] == [{"event": eid, "duplicate": False}]
    assert result["cursor"] == channel["source"].stat().st_size
    ledger, rid = channel["ledger"], channel["registrations"]["native"]
    assert ledger.readback(rid)["cursor"] == 0
    packet = ledger.delivery(rid, channel["target"])
    assert packet["event"] == eid
    assert packet["value"] == channel["frame"]
    assert ledger.ack(rid, eid, channel["target"])["cursor"] == 1


def lost_reply_command(channel):
    # Real subprocess crash after fsynced endpoint commit, before any reply.
    script = ("import json,os,sys; sys.path.insert(0,sys.argv[1]); "
              "from mesh_runner.native_result_channel import receive; "
              "receive(sys.argv[2],json.load(sys.stdin)); os._exit(23)")
    return [sys.executable, "-c", script, str(SCRIPTS), str(channel["endpoint"])]


def test_lost_reply_and_new_sender_process_replay_same_event(channel):
    append(channel)
    before = channel["journal"].readback()
    with pytest.raises(Unavailable):
        channel["journal"].ingest(RemoteSink(channel["sender"], lost_reply_command(channel)))
    assert channel["journal"].readback() == before
    script = ("import json,sys; sys.path.insert(0,sys.argv[1]); "
              "from mesh_runner.native_result_channel import RemoteSink; "
              "from mesh_runner.native_result_ingest import SourceJournal; "
              "print(json.dumps(SourceJournal(sys.argv[2]).ingest("
              "RemoteSink(sys.argv[3],json.loads(sys.argv[4])))))")
    restarted = subprocess.run([sys.executable, "-c", script, str(SCRIPTS),
                                str(channel["journal"].root), str(channel["sender"]),
                                json.dumps(channel["command"])], capture_output=True, check=True)
    assert json.loads(restarted.stdout)["events"] == [{"event": identity(channel["frame"]), "duplicate": True}]
    assert channel["ledger"].readback(channel["registrations"]["native"])["cursor"] == 0


@pytest.mark.parametrize("change", ["mac", "channel", "task", "session", "package", "test"])
def test_authenticated_scope_negatives_never_publish(channel, change):
    desc, key = load(channel["sender"])
    frame = json.loads(json.dumps(channel["frame"]))
    if change in ("task", "session", "package", "test"):
        field = {"session": "producer", "package": "package_sha256"}.get(change, change)
        frame["binding"][field] = (False if change == "test" else
                                   "c" * 64 if change == "package" else str(uuid4()))
    body = {"version": 1, "channel": "0" * 64 if change == "channel" else identity(desc),
            "kind": "native", "value": frame}
    envelope = signed(body, key)
    if change == "mac":
        envelope["mac"] = "0" * 64
    with pytest.raises(Refused):
        receive(channel["endpoint"], envelope)
    assert channel["ledger"].delivery(channel["registrations"]["native"], channel["target"]) is None


def test_failure_has_observer_identity_and_first_evidence_survives_lost_reply(channel):
    sink = RemoteSink(channel["sender"], lost_reply_command(channel))
    with pytest.raises(Unavailable):
        sink.fail("observe-source", "FileNotFoundError", "enrolled source was removed")
    desc, _ = load(channel["sender"])
    first = json.loads((channel["sender"] / "failure.json").read_text())
    assert first == json.loads((channel["endpoint"] / "failure.json").read_text())
    result = RemoteSink(channel["sender"], channel["command"]).fail("restart", "Changed", "different")
    assert result["duplicate"] is True
    rid = channel["registrations"]["failure"]
    packet = channel["ledger"].delivery(rid, channel["target"])
    assert packet["event"] == result["event"]
    assert packet["value"]["binding"] == failure_binding(desc)
    assert packet["value"]["binding"]["producer"] != channel["frame"]["binding"]["producer"]
    assert packet["value"]["owner"] == desc["owner"]
    assert packet["value"]["receipt_sha256"] == identity(first)
    assert packet["value"]["status"] == "unknown"
    assert channel["ledger"].delivery(channel["registrations"]["native"], channel["target"]) is None
    with pytest.raises(Refused, match="latched"):
        RemoteSink(channel["sender"], channel["command"]).publish(
            channel["frame"], desc["source"]["binding"]["producer"], desc["source"]["binding"]["task"])


def test_failure_blocks_later_native_on_receiver_without_consuming_its_sequence(channel):
    sink = RemoteSink(channel["sender"], channel["command"])
    sink.fail("observe-source", "Refused", "source replaced")
    desc, key = load(channel["sender"])
    request = signed({"version": 1, "channel": identity(desc), "kind": "native", "value": channel["frame"]}, key)
    with pytest.raises(Refused, match="latched"):
        receive(channel["endpoint"], request)
    assert channel["ledger"].readback(channel["registrations"]["native"])["cursor"] == 0


def test_private_key_symlink_and_permissions_refused(channel):
    key = channel["sender"] / "key"
    key.chmod(0o644)
    with pytest.raises(Refused, match="private owned"):
        RemoteSink(channel["sender"], channel["command"])
    key.chmod(0o600)
    backup = channel["sender"] / "saved-key"
    key.rename(backup)
    key.symlink_to(backup)
    with pytest.raises(OSError):
        RemoteSink(channel["sender"], channel["command"])


def test_wrong_peer_key_does_not_advance_source_cursor(channel):
    append(channel)
    other = channel["endpoint"] / "key"
    other.write_bytes(b"x" * 32)
    before = channel["journal"].readback()
    with pytest.raises(Unavailable):
        channel["journal"].ingest(RemoteSink(channel["sender"], channel["command"]))
    assert channel["journal"].readback() == before
    assert channel["ledger"].delivery(channel["registrations"]["native"], channel["target"]) is None


@pytest.mark.parametrize("status", ["failed", "dead", "unknown"])
def test_original_native_terminal_frame_keeps_original_provenance(channel, status):
    channel["frame"].update(status=status, reason="producer supplied terminal grounds")
    append(channel)
    channel["journal"].ingest(RemoteSink(channel["sender"], channel["command"]))
    packet = channel["ledger"].delivery(channel["registrations"]["native"], channel["target"])
    assert packet["value"] == channel["frame"]
    assert packet["value"]["binding"]["producer"] == channel["frame"]["binding"]["producer"]
    assert channel["ledger"].delivery(channel["registrations"]["failure"], channel["target"]) is None


def waiting_channel(channel):
    channels = channel["endpoint"].parent / "channels"
    channels.mkdir(mode=0o700)
    endpoint = channels / channel["registrations"]["native"]
    channel["endpoint"].rename(endpoint)
    channel["endpoint"] = endpoint
    channel["command"][-1] = str(endpoint)
    target, owner = channel["target"], str(uuid4())
    attempt = {"id": target["attempt"], "task_id": target["task"], "session_id": target["session"],
               "state": "waiting", "mode": "work", "checkout": {},
               "wait": {"native_receiver": target}}
    task = {"id": target["task"], "assignee_id": owner, "assignee_type": "agent",
            "checked_out_by": None, "checkout_generation": target["generation"],
            "checkout_session_id": target["attempt"]}
    return channels, attempt, task, owner


def test_registered_wait_prioritizes_authenticated_observer_failure_and_requires_repair(channel):
    channels, attempt, task, owner = waiting_channel(channel)
    sink = RemoteSink(channel["sender"], channel["command"])
    append(channel)
    channel["journal"].ingest(sink)
    sink.fail("observe-source", "Refused", "original enrolled source was replaced")
    primary = channel["registrations"]["native"]
    packet = observe(channel["ledger"], primary, attempt, task, owner, channels=channels)
    assert packet["registration"] == channel["registrations"]["failure"]
    assert "original enrolled source was replaced" in packet["value"]["reason"]
    assert packet["value"]["binding"]["producer"] != channel["frame"]["binding"]["producer"]
    assert channel["ledger"].readback(primary)["cursor"] == 0
    channel["ledger"].ack(packet["registration"], packet["event"], channel["target"])
    with pytest.raises(Refused, match="explicit repair"):
        observe(channel["ledger"], primary, attempt, task, owner, channels=channels)


@pytest.mark.parametrize("change", ["evidence-lost", "evidence-changed", "receiver-changed", "channel-lost"])
def test_wait_does_not_ignore_missing_or_changed_observer_evidence(channel, change):
    channels, attempt, task, owner = waiting_channel(channel)
    sink = RemoteSink(channel["sender"], channel["command"])
    sink.fail("observe-source", "Refused", "source integrity refused")
    evidence = channel["endpoint"] / "failure.json"
    if change == "evidence-lost":
        evidence.unlink()
    elif change == "evidence-changed":
        value = json.loads(evidence.read_text())
        value["reason"] = "different grounds"
        evidence.write_text(json.dumps(value))
    elif change == "receiver-changed":
        task["checkout_generation"] += 1
    else:
        (channel["endpoint"] / "channel.json").unlink()
    with pytest.raises((Refused, OSError)):
        observe(channel["ledger"], channel["registrations"]["native"], attempt, task, owner, channels=channels)


def test_receiver_recovers_persisted_failure_evidence_before_failed_ledger_commit(channel):
    channels, attempt, task, owner = waiting_channel(channel)
    sink = RemoteSink(channel["sender"], channel["command"])
    ledger = channel["ledger"]
    ledger.root.chmod(0o500)
    try:
        with pytest.raises(Unavailable):
            sink.fail("observe-source", "Refused", "source identity lost")
    finally:
        ledger.root.chmod(0o700)
    assert (channel["endpoint"] / "failure.json").is_file()
    assert ledger.delivery(channel["registrations"]["failure"], channel["target"]) is None
    with pytest.raises(Refused, match="awaiting durable delivery"):
        observe(ledger, channel["registrations"]["native"], attempt, task, owner, channels=channels)
    repaired = RemoteSink(channel["sender"], channel["command"]).fail("restart", "Changed", "different grounds")
    assert repaired["duplicate"] is False
    packet = observe(ledger, channel["registrations"]["native"], attempt, task, owner, channels=channels)
    assert packet["event"] == repaired["event"]
    assert "source identity lost" in packet["value"]["reason"]

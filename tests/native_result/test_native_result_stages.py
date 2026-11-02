"""Real-file stage cutovers retain provenance, old cursors and pending ACKs."""
import copy
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_channel import RemoteSink, bootstrap, descriptor, install, load
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger, Refused, identity
from mesh_runner.native_result_lineage import MAX_LINEAGE, validate, verify_local
from mesh_runner.native_result_source import MARKER


def append(path, contract, owner, sequence=1):
    frame = {"binding": contract, "sequence": sequence, "status": "result",
             "receipt_sha256": "b" * 64, "owner": owner, "reason": ""}
    row = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": MARKER + json.dumps(frame)}]}}
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    return frame


@pytest.fixture
def stage(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "freeze",
                "package_sha256": "a" * 64, "test": True}
    owner = str(uuid4())
    source = root / "native.jsonl"
    source.write_text(json.dumps({"type": "session_meta",
                                 "payload": {"id": contract["producer"]}}) + "\n")
    journal = SourceJournal.initialize(tmp_path / "freeze", root, source, contract)
    ledger = Ledger.initialize(tmp_path / "ledger")
    target = {"task": str(uuid4()), "attempt": str(uuid4()), "session": str(uuid4()), "generation": 1}
    rid = ledger.register(contract, target)["registration"]
    frame = append(source, contract, owner)
    journal.ingest(ledger)
    return locals()


def next_stage(t, previous=None, contract=None, directory="pre-change", owner=None):
    return SourceJournal.initialize_stage(
        t["tmp_path"] / directory, previous or t["journal"],
        contract or dict(t["contract"], stage="pre-change"), owner or t["owner"], "c" * 64)


def test_freeze_pre_change_actual_preserves_each_pending_ack_and_cursor(stage):
    t = stage
    frozen = t["journal"].enrollment()
    frozen_bytes = (t["journal"].root / "source.json").read_bytes()
    pending = t["ledger"].delivery(t["rid"], t["target"])
    pre = next_stage(t)
    pre_contract = pre.readback()["binding"]
    pre_rid = t["ledger"].register(pre_contract, t["target"])["registration"]
    assert pre.ingest(t["ledger"])["events"] == []
    pre_frame = append(t["source"], pre_contract, t["owner"])
    pre.ingest(t["ledger"])
    pre_state = pre.enrollment()
    pre_pending = t["ledger"].delivery(pre_rid, t["target"])

    actual_contract = dict(pre_contract, stage="actual", package_sha256="d" * 64)
    actual = next_stage(t, pre, actual_contract, "actual")
    state = actual.enrollment()
    assert state["version"] == 2 and state["lineage"]["version"] == 2
    assert state["lineage"]["previous"] == pre_state["source"]
    assert state["lineage"]["previous_lineage"] == pre_state["lineage"]
    assert state["lineage"]["previous_lineage"]["previous"] == frozen["source"]
    assert state["source"]["cursor"] == t["source"].stat().st_size
    assert state["source"]["inode"] == frozen["source"]["inode"]
    actual_rid = t["ledger"].register(actual_contract, t["target"])["registration"]
    assert actual.ingest(t["ledger"])["events"] == []
    actual_frame = append(t["source"], actual_contract, t["owner"])
    # Historical bindings must not scan the new stage's suffix, even on restart.
    assert verify_local(state["lineage"], state["source"]) == state["lineage"]
    SourceJournal(actual.root).ingest(t["ledger"])
    assert t["ledger"].delivery(actual_rid, t["target"])["value"] == actual_frame
    assert t["ledger"].delivery(pre_rid, t["target"]) == pre_pending
    assert pre_pending["value"] == pre_frame
    assert t["ledger"].delivery(t["rid"], t["target"]) == pending
    assert pre.enrollment() == pre_state
    assert t["journal"].enrollment() == frozen
    assert (t["journal"].root / "source.json").read_bytes() == frozen_bytes
    assert t["ledger"].ack(actual_rid, identity(actual_frame), t["target"])["cursor"] == 1
    assert t["ledger"].readback(pre_rid)["cursor"] == 0
    assert t["ledger"].readback(t["rid"])["cursor"] == 0


@pytest.mark.parametrize("changes", [{"stage": "pre-change"}, {"package_sha256": "d" * 64},
                                   {"stage": "actual", "package_sha256": "d" * 64}])
def test_stage_or_package_can_change_independently(stage, changes):
    t = stage
    following = next_stage(t, contract=dict(t["contract"], **changes))
    assert following.readback()["binding"] == dict(t["contract"], **changes)
    assert following.enrollment()["lineage"]["previous_lineage"] is None


@pytest.mark.parametrize("change", ["unchanged", "producer", "task", "test", "stage", "package"])
def test_invalid_stage_contract_leaves_previous_and_pending_unchanged(stage, change):
    t = stage
    old = t["journal"].enrollment()
    pending = t["ledger"].delivery(t["rid"], t["target"])
    contract = dict(t["contract"], stage="pre-change")
    if change == "unchanged":
        contract = dict(t["contract"])
    elif change in ("producer", "task"):
        contract[change] = str(uuid4())
    elif change == "test":
        contract["test"] = False
    elif change == "stage":
        contract["stage"] = "PRE-CHANGE"
    else:
        contract["package_sha256"] = "invalid"
    with pytest.raises(Refused):
        next_stage(t, contract=contract)
    assert not (t["tmp_path"] / "pre-change").exists()
    assert t["journal"].enrollment() == old
    assert t["ledger"].delivery(t["rid"], t["target"]) == pending


@pytest.mark.parametrize("change", ["undrained", "partial", "replace", "metadata", "truncate"])
def test_actual_source_must_be_unchanged_and_drained_to_complete_eof(stage, change):
    t = stage
    old = t["journal"].enrollment()
    if change == "undrained":
        append(t["source"], t["contract"], t["owner"], 2)
    elif change == "partial":
        with t["source"].open("a") as stream:
            stream.write('{"type":')
    elif change == "replace":
        replacement = t["root"] / "replacement"
        replacement.write_bytes(t["source"].read_bytes())
        replacement.replace(t["source"])
    elif change == "metadata":
        raw = t["source"].read_text()
        t["source"].write_text(raw.replace('"type": "session_meta"', '"type":  "session_meta"'))
    else:
        t["source"].write_bytes(t["source"].read_bytes()[:-1])
    with pytest.raises(Refused):
        next_stage(t)
    assert t["journal"].enrollment() == old
    assert not (t["tmp_path"] / "pre-change").exists()


def test_stage_after_real_producer_succession_keeps_original_provenance(stage):
    t = stage
    successor = str(uuid4())
    new_source = t["root"] / "successor.jsonl"
    new_source.write_text(json.dumps({"type": "session_meta", "payload": {"id": successor}}) + "\n")
    following = SourceJournal.initialize_successor(t["tmp_path"] / "successor", t["journal"],
        t["root"], new_source, successor, t["owner"], "d" * 64)
    succession = following.enrollment()
    contract = dict(following.readback()["binding"], stage="pre-change")
    pre = next_stage(t, following, contract)
    actual = next_stage(t, pre, dict(contract, stage="actual"), "actual")
    state = actual.enrollment()
    assert state["lineage"]["previous_lineage"]["previous_lineage"] == succession["lineage"]
    assert state["lineage"]["previous"]["binding"]["producer"] == successor
    original = state["lineage"]["previous_lineage"]["previous_lineage"]["previous"]
    assert original == t["journal"].readback()
    append(new_source, state["source"]["binding"], t["owner"])
    verify_local(state["lineage"], state["source"])
    # The original producer's historical EOF fence still applies through v2.
    append(t["source"], t["contract"], t["owner"], 2)
    with pytest.raises(Refused, match="historical source changed"):
        verify_local(state["lineage"], state["source"])
    with pytest.raises(Refused, match="historical source changed"):
        actual.ingest(t["ledger"])
    assert following.enrollment() == succession


@pytest.mark.parametrize("change", ["owner", "uuid", "authority"])
def test_operator_is_valid_and_constant_across_chain(stage, change):
    t = stage
    pre = next_stage(t)
    owner = str(uuid4()) if change == "owner" else "not-a-uuid" if change == "uuid" else t["owner"]
    authority = "wrong" if change == "authority" else "d" * 64
    with pytest.raises(Refused):
        SourceJournal.initialize_stage(t["tmp_path"] / "actual", pre,
            dict(pre.readback()["binding"], stage="actual"), owner, authority)
    assert not (t["tmp_path"] / "actual").exists()


@pytest.mark.parametrize("change", ["inode", "path", "producer", "task", "test", "cursor",
                                   "unchanged", "extra", "version", "operator", "authority",
                                   "broken-chain"])
def test_stage_lineage_strict_shape_and_provenance(stage, change):
    t = stage
    pre = next_stage(t)
    actual = next_stage(t, pre, dict(pre.readback()["binding"], stage="actual"), "actual")
    state = actual.enrollment()
    lineage = copy.deepcopy(state["lineage"])
    previous = lineage["previous"]
    if change == "inode":
        previous["inode"] += 1
    elif change == "path":
        previous["source"] = str(t["root"] / "other.jsonl")
    elif change in ("producer", "task"):
        previous["binding"][change] = str(uuid4())
    elif change == "test":
        previous["binding"]["test"] = False
    elif change == "cursor":
        previous["cursor"] -= 1
    elif change == "unchanged":
        previous["binding"] = dict(lineage["successor"]["binding"])
    elif change == "extra":
        lineage["unexpected"] = True
    elif change == "version":
        lineage["version"] = True
    elif change == "operator":
        lineage["previous_lineage"]["operator"] = str(uuid4())
    elif change == "authority":
        lineage["authority_sha256"] = "not-a-hash"
    else:
        lineage["previous_lineage"]["successor"]["binding"]["package_sha256"] = "d" * 64
    with pytest.raises(Refused):
        validate(lineage, state["source"])


def test_lineage_has_bounded_depth_and_excess_cannot_create_a_journal(stage):
    t = stage
    previous = t["journal"]
    for number in range(MAX_LINEAGE):
        previous = next_stage(t, previous,
            dict(t["contract"], stage=f"stage-{number}"), f"stage-{number}")
    state = previous.enrollment()
    verify_local(state["lineage"], state["source"])
    with pytest.raises(Refused, match="bounded depth"):
        next_stage(t, previous, dict(t["contract"], stage="excess"), "excess")
    assert previous.enrollment() == state
    assert not (t["tmp_path"] / "excess").exists()


@pytest.mark.parametrize("change", ["boundary", "truncated", "metadata", "replaced"])
def test_historical_stage_pins_are_checked_without_scanning_later_binding(stage, change):
    t = stage
    pre = next_stage(t)
    state = pre.enrollment()
    boundary = state["lineage"]["previous"]["cursor"]
    append(t["source"], state["source"]["binding"], t["owner"])
    raw = t["source"].read_bytes()
    if change == "boundary":
        raw = raw[:boundary - 1] + b" " + raw[boundary:]
    elif change == "truncated":
        raw = raw[:boundary - 1]
    elif change == "metadata":
        raw = raw.replace(b'"session_meta"', b'"session_META"', 1)
    else:
        replacement = t["root"] / "replacement"
        replacement.write_bytes(raw)
        replacement.replace(t["source"])
    if change != "replaced":
        t["source"].write_bytes(raw)
    with pytest.raises(Refused):
        verify_local(state["lineage"], state["source"])
    with pytest.raises(Refused):
        pre.ingest(t["ledger"])
    assert pre.enrollment() == state


def test_active_stage_rejects_late_old_binding_without_advancing_cursor(stage):
    t = stage
    pre = next_stage(t)
    state = pre.enrollment()
    append(t["source"], t["contract"], t["owner"], 2)
    with pytest.raises(Refused, match="binding mismatch"):
        pre.ingest(t["ledger"])
    assert pre.enrollment() == state


def test_stage_cannot_be_forged_into_distinct_producer_succession(stage):
    t = stage
    pre = next_stage(t)
    state = pre.enrollment()
    forged = copy.deepcopy(state["lineage"])
    forged["version"] = 1
    del forged["previous_lineage"]
    with pytest.raises(Refused, match="change actual source"):
        validate(forged, state["source"])
    with pytest.raises(Refused, match="change actual source"):
        SourceJournal.initialize_successor(t["tmp_path"] / "alias", t["journal"],
            t["root"], t["source"], t["contract"]["producer"], t["owner"], "c" * 64)
    assert not (t["tmp_path"] / "alias").exists()


def installed_channel(t, journal, name):
    sender, endpoint = t["tmp_path"] / (name + "-sender"), t["tmp_path"] / (name + "-endpoint")
    bootstrap(sender, journal, t["target"], str(uuid4()), t["owner"])
    registrations = install(endpoint, t["ledger"], sender)
    command = [sys.executable, "-m", "mesh_runner.native_result_channel",
               "receive", "--channel", str(endpoint)]
    return sender, registrations, RemoteSink(sender, command)


def test_real_channels_deliver_each_same_file_stage_without_consuming_old_pending(stage):
    t = stage
    old_pending = t["ledger"].delivery(t["rid"], t["target"])
    freeze_sender, freeze_reg, freeze_sink = installed_channel(t, t["journal"], "freeze")
    assert freeze_reg["native"] == t["rid"]
    freeze_frame = append(t["source"], t["contract"], t["owner"], 2)
    assert t["journal"].ingest(freeze_sink)["events"] == [
        {"event": identity(freeze_frame), "duplicate": False}]
    freeze_state = t["journal"].enrollment()
    frozen_registration = t["ledger"].readback(t["rid"])

    pre = next_stage(t)
    pre_sender, pre_reg, pre_sink = installed_channel(t, pre, "pre-change")
    pre_desc, _ = load(pre_sender)
    assert pre_desc["version"] == 2
    assert pre_desc["lineage"] == pre.enrollment()["lineage"]
    assert pre.ingest(pre_sink)["events"] == []
    pre_frame = append(t["source"], pre.readback()["binding"], t["owner"])
    assert pre.ingest(pre_sink)["events"] == [{"event": identity(pre_frame), "duplicate": False}]
    pre_pending = t["ledger"].delivery(pre_reg["native"], t["target"])
    pre_registration = t["ledger"].readback(pre_reg["native"])
    pre_state = pre.enrollment()

    actual = next_stage(t, pre,
        dict(pre.readback()["binding"], stage="actual", package_sha256="d" * 64), "actual")
    actual_sender, actual_reg, actual_sink = installed_channel(t, actual, "actual")
    actual_desc, _ = load(actual_sender)
    assert actual_desc["version"] == 2
    assert actual_desc["lineage"]["previous_lineage"] == pre_state["lineage"]
    assert actual.ingest(actual_sink)["events"] == []
    actual_frame = append(t["source"], actual.readback()["binding"], t["owner"])
    # A stale stage channel cannot authenticate the active stage's frame.
    before = actual.enrollment()
    with pytest.raises(Refused, match="source channel producer/task mismatch"):
        freeze_sink.publish(actual_frame, t["contract"]["producer"], t["contract"]["task"])
    assert actual.enrollment() == before
    assert SourceJournal(actual.root).ingest(actual_sink)["events"] == [
        {"event": identity(actual_frame), "duplicate": False}]
    assert actual.ingest(actual_sink)["events"] == []
    assert t["ledger"].delivery(actual_reg["native"], t["target"])["value"] == actual_frame
    assert t["ledger"].delivery(pre_reg["native"], t["target"]) == pre_pending
    assert t["ledger"].delivery(t["rid"], t["target"]) == old_pending
    assert t["ledger"].readback(t["rid"]) == frozen_registration
    assert t["ledger"].readback(pre_reg["native"]) == pre_registration
    assert t["journal"].enrollment() == freeze_state
    assert pre.enrollment() == pre_state
    for registration in (freeze_reg, pre_reg, actual_reg):
        assert t["ledger"].delivery(registration["failure"], t["target"]) is None

    # Only explicit receiver ACKs consume an individual stage's sequence.
    t["ledger"].ack(actual_reg["native"], identity(actual_frame), t["target"])
    assert t["ledger"].readback(t["rid"]) == frozen_registration
    assert t["ledger"].readback(pre_reg["native"]) == pre_registration
    t["ledger"].ack(t["rid"], old_pending["event"], t["target"])
    assert t["ledger"].delivery(t["rid"], t["target"])["value"] == freeze_frame
    t["ledger"].ack(pre_reg["native"], pre_pending["event"], t["target"])
    assert t["ledger"].delivery(pre_reg["native"], t["target"]) is None
    assert load(freeze_sender)[0]["source"]["binding"] == t["contract"]


@pytest.mark.parametrize("observer", ["original", "successor"])
def test_channel_rejects_any_historical_producer_after_succession_and_stages(stage, observer):
    t = stage
    successor = str(uuid4())
    source = t["root"] / "successor.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": successor}}) + "\n")
    following = SourceJournal.initialize_successor(t["tmp_path"] / "successor", t["journal"],
        t["root"], source, successor, t["owner"], "d" * 64)
    pre = next_stage(t, following, dict(following.readback()["binding"], stage="pre-change"))
    actual = next_stage(t, pre, dict(pre.readback()["binding"], stage="actual"), "actual")
    forbidden = t["contract"]["producer"] if observer == "original" else successor
    rejected_sender = t["tmp_path"] / "rejected-sender"
    with pytest.raises(Refused, match="distinct"):
        bootstrap(rejected_sender, actual, t["target"], forbidden, t["owner"])
    assert not rejected_sender.exists()

    sender = t["tmp_path"] / "sender"
    bootstrap(sender, actual, t["target"], str(uuid4()), t["owner"])
    desc, _ = load(sender)
    assert desc["version"] == 2
    original = desc["lineage"]["previous_lineage"]["previous_lineage"]["previous"]
    assert original["binding"]["producer"] == t["contract"]["producer"]
    desc["observer"] = forbidden
    with pytest.raises(Refused, match="distinct"):
        descriptor(desc)
    # The receiver independently validates a privately transferred descriptor.
    (sender / "channel.json").write_text(json.dumps(desc))
    before = t["ledger"].readback(t["rid"])
    with pytest.raises(Refused, match="distinct"):
        install(t["tmp_path"] / "rejected-endpoint", t["ledger"], sender)
    assert not (t["tmp_path"] / "rejected-endpoint").exists()
    assert t["ledger"].readback(t["rid"]) == before

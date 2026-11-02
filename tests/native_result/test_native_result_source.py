import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ledger import Ledger, Refused
from mesh_runner.native_result_source import MARKER, enroll, scan


def line(value):
    return json.dumps(value).encode() + b"\n"


@pytest.fixture
def original(tmp_path):
    root = tmp_path / "native-sessions"
    root.mkdir()
    source = root / "rollout.jsonl"
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    source.write_bytes(line({"type": "session_meta", "payload": {"id": contract["producer"]}}))
    registration = enroll(root, source, contract)
    frame = {"binding": contract, "sequence": 1, "status": "result",
             "receipt_sha256": "b" * 64, "owner": "Riker", "reason": ""}
    return root, source, registration, frame


def emission(frame, role="assistant"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "output_text", "text": MARKER + json.dumps(frame)}]}}


def test_native_provenance_to_ledger_cursor_restart_and_ack(original, tmp_path):
    root, source, reg, frame = original
    with source.open("ab") as output:
        output.write(line(emission(frame, "user")))
        output.write(line(emission(frame, "tool")))
        output.write(line(emission(frame)))
    batch = scan(reg)
    assert batch["frames"] == [frame]
    ledger = Ledger.initialize(tmp_path / "mailbox")
    receiver = {"task": frame["binding"]["task"], "attempt": str(uuid4()),
                "session": str(uuid4()), "generation": 1}
    rid = ledger.register(frame["binding"], receiver)["registration"]
    published = ledger.publish(batch["frames"][0], batch["producer"], batch["task"])
    # Crash before source cursor commit: source replay is the same envelope.
    replay = scan(reg)
    duplicate = Ledger(ledger.root).publish(replay["frames"][0], replay["producer"], replay["task"])
    assert duplicate == dict(published, duplicate=True)
    reg["cursor"] = batch["cursor"]
    assert scan(reg)["frames"] == []
    delivered = ledger.delivery(rid, receiver)
    assert delivered["event"] == published["event"]
    assert ledger.ack(rid, delivered["event"], receiver)["cursor"] == 1


def test_wrong_original_native_session_cannot_enroll(original):
    root, source, reg, frame = original
    wrong = dict(reg["binding"], producer=str(uuid4()))
    with pytest.raises(Refused, match="original native producer mismatch"):
        enroll(root, source, wrong)


def test_wrong_task_or_digest_in_native_output_refused(original):
    root, source, reg, frame = original
    frame["binding"] = dict(frame["binding"], task=str(uuid4()))
    with source.open("ab") as output:
        output.write(line(emission(frame)))
    with pytest.raises(Refused, match="native frame binding mismatch"):
        scan(reg)


def test_partial_append_not_consumed(original):
    root, source, reg, frame = original
    raw = line(emission(frame))
    with source.open("ab") as output:
        output.write(raw[:-1])
    assert scan(reg) == {"cursor": reg["cursor"], "frames": [],
                         "producer": reg["binding"]["producer"], "task": reg["binding"]["task"]}
    with source.open("ab") as output:
        output.write(raw[-1:])
    assert scan(reg)["frames"] == [frame]


def test_source_replacement_and_truncation_refused(original):
    root, source, reg, frame = original
    saved = source.read_bytes()
    source.rename(root / "old.jsonl")
    source.write_bytes(saved)
    with pytest.raises(Refused, match="source replaced"):
        scan(reg)
    source.unlink()
    (root / "old.jsonl").rename(source)
    with source.open("ab") as output:
        output.write(line(emission(frame)))
    reg["cursor"] = scan(reg)["cursor"]
    source.write_bytes(saved)
    with pytest.raises(Refused, match="truncated"):
        scan(reg)


def test_quoted_and_event_message_frames_do_not_authenticate(original):
    root, source, reg, frame = original
    quoted = emission(frame)
    quoted["payload"]["content"][0]["text"] = "Example: " + quoted["payload"]["content"][0]["text"]
    with source.open("ab") as output:
        output.write(line(quoted))
        output.write(line({"type": "event_msg", "payload": {"type": "agent_message", "message": MARKER + json.dumps(frame)}}))
    assert scan(reg)["frames"] == []


def test_symlink_and_outside_root_refused(original, tmp_path):
    root, source, reg, frame = original
    link = root / "link.jsonl"
    link.symlink_to(source)
    with pytest.raises(Refused, match="source symlink"):
        enroll(root, link, frame["binding"])
    with pytest.raises(Refused, match="outside enrolled root"):
        enroll(tmp_path / "different", source, frame["binding"])
    directory = root / "linked-directory"
    directory.symlink_to(root, target_is_directory=True)
    with pytest.raises(Refused, match="source symlink"):
        enroll(root, directory / source.name, frame["binding"])


@pytest.mark.parametrize("metadata", [None, [], "invalid", 17])
def test_malformed_metadata_fails_closed(original, metadata):
    root, source, reg, frame = original
    source.write_bytes(line(metadata))
    with pytest.raises(Refused, match="invalid native metadata"):
        enroll(root, source, frame["binding"])

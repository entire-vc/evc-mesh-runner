import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ingest import SourceJournal
from mesh_runner.native_result_ledger import Ledger, Refused
from mesh_runner.native_result_source import MARKER


@pytest.fixture
def setup(tmp_path):
    native = tmp_path / "sessions"
    native.mkdir()
    contract = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "actual023",
                "package_sha256": "a" * 64, "test": True}
    source = native / "native.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": contract["producer"]}}) + "\n")
    journal = SourceJournal.initialize(tmp_path / "cursor", native, source, contract)
    ledger = Ledger.initialize(tmp_path / "ledger")
    receiver = {"task": str(uuid4()), "attempt": str(uuid4()), "session": str(uuid4()), "generation": 1}
    rid = ledger.register(contract, receiver)["registration"]
    frame = {"binding": contract, "sequence": 1, "status": "result", "receipt_sha256": "b" * 64,
             "owner": "Steward", "reason": ""}
    with source.open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": MARKER + json.dumps(frame)}]}}) + "\n")
    return journal, ledger, source, rid, receiver


def test_cursor_fsync_failure_replays_committed_outbox_after_process_restart(setup):
    journal, ledger, source, rid, receiver = setup
    before = journal.readback()
    # True filesystem boundary: outbox is writable, source cursor cannot commit.
    journal.root.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            journal.ingest(ledger)
    finally:
        journal.root.chmod(0o700)
    assert journal.readback() == before
    pending = ledger.delivery(rid, receiver)
    assert pending is not None
    assert ledger.readback(rid)["cursor"] == 0
    script = ("import json,sys; from mesh_runner.native_result_ingest import SourceJournal; "
              "from mesh_runner.native_result_ledger import Ledger; "
              "print(json.dumps(SourceJournal(sys.argv[1]).ingest(Ledger(sys.argv[2]))))")
    process = subprocess.run([sys.executable, "-c", script, str(journal.root), str(ledger.root)],
                             cwd=SCRIPTS, check=True,
                             capture_output=True, text=True)
    result = json.loads(process.stdout)
    assert result["events"] == [{"event": pending["event"], "duplicate": True}]
    assert journal.readback()["cursor"] == source.stat().st_size
    assert journal.ingest(ledger)["events"] == []
    assert ledger.delivery(rid, receiver) == pending
    assert ledger.ack(rid, pending["event"], receiver)["cursor"] == 1


def test_lost_or_corrupt_source_cursor_never_reenrolls(setup):
    journal, ledger, source, rid, receiver = setup
    contract = journal.readback()["binding"]
    state = journal.root / "source.json"
    state.write_text("broken")
    with pytest.raises(Refused, match="corrupt"):
        journal.ingest(ledger)
    assert ledger.delivery(rid, receiver) is None
    state.unlink()
    with pytest.raises(FileNotFoundError):
        journal.ingest(ledger)
    with pytest.raises(FileExistsError):
        SourceJournal.initialize(journal.root, source.parent, source, contract)


def test_state_symlink_or_public_permissions_cannot_ingest(setup):
    journal, ledger, source, rid, receiver = setup
    state = journal.root / "source.json"
    backup = journal.root / "saved.json"
    state.rename(backup)
    state.symlink_to(backup)
    with pytest.raises(OSError):
        journal.ingest(ledger)
    state.unlink()
    backup.rename(state)
    state.chmod(0o644)
    with pytest.raises(Refused, match="private owned source state"):
        journal.ingest(ledger)
    assert ledger.delivery(rid, receiver) is None

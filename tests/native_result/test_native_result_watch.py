"""Actual Darwin kernel file events and actual child-process stdio transport."""
import json
from pathlib import Path
import select
import selectors
import subprocess
import sys

import pytest

from test_native_result_channel import SCRIPTS, append, channel
from mesh_runner.native_result_channel import save_private
from mesh_runner.native_result_ledger import identity

pytestmark = pytest.mark.skipif(not hasattr(select, "kqueue"), reason="Darwin/BSD kqueue required")


def line(process):
    with selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        assert ready.select(5), "no observer event within test deadline"
    raw = process.stdout.readline()
    assert raw, "observer exited before event"
    return json.loads(raw)


def start(channel):
    command_file = channel["sender"] / "receiver-command.json"
    save_private(command_file, json.dumps(channel["command"]).encode())
    return subprocess.Popen([sys.executable, "-m", "mesh_runner.native_result_watch",
                             "--journal", str(channel["journal"].root),
                             "--channel", str(channel["sender"]),
                            "--receiver-command", str(command_file)],
                            # Keep readline from hiding the next complete event
                            # in Python's buffer while select watches the OS fd.
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def test_real_vnode_append_delivers_and_restart_drains_unobserved_tail(channel):
    process = start(channel)
    try:
        assert line(process) == {"event": "observing"}
        append(channel)
        seen = line(process)
        assert seen["event"] == "ingested" and seen["frames"] == 1
        rid = channel["registrations"]["native"]
        pending = channel["ledger"].delivery(rid, channel["target"])
        assert pending["event"] == identity(channel["frame"])
        assert channel["ledger"].ack(rid, pending["event"], channel["target"])["cursor"] == 1
    finally:
        stop(process)
    next_frame = {**channel["frame"], "sequence": 2, "receipt_sha256": "c" * 64}
    append(channel, next_frame)
    restarted = start(channel)
    try:
        assert line(restarted)["event"] == "ingested"
        assert line(restarted) == {"event": "observing"}
        packet = channel["ledger"].delivery(rid, channel["target"])
        assert packet["event"] == identity(next_frame)
        assert channel["ledger"].readback(rid)["cursor"] == 1
    finally:
        stop(restarted)


@pytest.mark.parametrize("change", ["source-rename", "directory-rename", "truncate", "malformed"])
def test_real_vnode_integrity_failure_delivers_observer_unknown(channel, change):
    process = start(channel)
    try:
        assert line(process) == {"event": "observing"}
        source = channel["source"]
        if change == "source-rename":
            source.rename(source.with_suffix(".replaced"))
        elif change == "directory-rename":
            source.parent.rename(source.parent.with_name("renamed-sessions"))
        elif change == "truncate":
            source.write_text("")
        else:
            with source.open("a") as stream:
                stream.write("malformed record\n")
        failure = line(process)
        assert failure["event"] == "unknown-delivered"
        assert process.wait(timeout=5) == 2
        packet = channel["ledger"].delivery(channel["registrations"]["failure"], channel["target"])
        assert packet["event"] == failure["receipt"]
        assert packet["value"]["status"] == "unknown"
        assert packet["value"]["binding"]["producer"] != channel["frame"]["binding"]["producer"]
        assert channel["journal"].readback()["cursor"] < 1024
    finally:
        stop(process)


def test_graceful_observer_stop_is_unknown_and_restart_repeats_same_evidence(channel):
    process = start(channel)
    try:
        assert line(process) == {"event": "observing"}
        process.terminate()
        failure = line(process)
        assert failure["event"] == "unknown-delivered"
        assert process.wait(timeout=5) == 2
    finally:
        stop(process)
    restarted = start(channel)
    try:
        repeated = line(restarted)
        assert repeated["receipt"] == failure["receipt"]
        assert repeated["duplicate"] is True
        assert restarted.wait(timeout=5) == 2
    finally:
        stop(restarted)

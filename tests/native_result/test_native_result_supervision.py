"""Real owned process lifetimes, kqueue, source files and receiver commits."""
import json
import os
import select
import signal
import subprocess
import sys
from uuid import uuid4

import pytest

from test_native_result_channel import SCRIPTS, append, channel
from test_native_result_watch import line
from mesh_runner.native_result_channel import bootstrap, install, save_private
from mesh_runner.native_result_process import snapshot

pytestmark = pytest.mark.skipif(not hasattr(select, "kqueue"), reason="Darwin/BSD kqueue required")


@pytest.fixture
def supervised(channel):
    # This is our own fixture producer, never a live native session/process.
    native = subprocess.Popen([sys.executable, "-c",
                              "import os,sys;print('ready',flush=True);sys.stdin.read(1);os.execv(sys.executable,[sys.executable,'-c','import sys;sys.stdin.read()'])"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert native.stdout.readline() == b"ready\n"
    old = channel["sender"]
    sender, endpoint = old.parent / "supervised-source", old.parent / "supervised-receiver"
    bootstrap(sender, channel["journal"], channel["target"], str(uuid4()), str(uuid4()),
              process=snapshot(native.pid), authority_sha256="c" * 64)
    channel.update(sender=sender, endpoint=endpoint,
                   registrations=install(endpoint, channel["ledger"], sender))
    command = [sys.executable, "-m", "mesh_runner.native_result_channel",
               "receive", "--channel", str(endpoint)]
    command_file = sender / "receiver-command.json"
    save_private(command_file, json.dumps(command).encode())
    channel.update(command=command, command_file=command_file, native=native)
    yield channel
    if native.poll() is None:
        native.kill()
    native.communicate(timeout=5)


def start(channel):
    return subprocess.Popen([sys.executable, "-m", "mesh_runner.native_result_watch",
                             "--journal", str(channel["journal"].root),
                             "--channel", str(channel["sender"]),
                             "--receiver-command", str(channel["command_file"]), "--supervise"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             bufsize=0, start_new_session=True)


def failure(channel):
    packet = channel["ledger"].delivery(channel["registrations"]["failure"], channel["target"])
    assert packet["value"]["status"] == "unknown"
    assert packet["value"]["binding"]["producer"] != channel["frame"]["binding"]["producer"]
    assert packet["value"]["owner"]
    assert packet["value"]["reason"]
    assert channel["ledger"].readback(channel["registrations"]["native"])["cursor"] == 0
    return packet


def finish(process):
    # Kill only the test's isolated supervisor group, including its own worker.
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=5)


def test_pinned_producer_exit_delivers_observer_unknown_and_restart_repeats(supervised):
    p = start(supervised)
    try:
        assert line(p) == {"event": "observing"}
        supervised["native"].terminate()
        supervised["native"].wait(timeout=5)
        sent = line(p)
        assert sent["event"] == "unknown-delivered"
        assert p.wait(timeout=5) == 2
        packet = failure(supervised)
        assert "native process lifetime ended" in packet["value"]["reason"]
    finally:
        finish(p)
    again = start(supervised)
    try:
        repeated = line(again)
        assert repeated["receipt"] == sent["receipt"] and repeated["duplicate"] is True
        assert again.wait(timeout=5) == 2
    finally:
        finish(again)


def test_worker_sigkill_is_observed_by_supervisor(supervised):
    p = start(supervised)
    try:
        assert line(p) == {"event": "observing"}
        state = json.loads((supervised["sender"] / "supervisor.json").read_text())
        os.kill(state["worker"]["pid"], signal.SIGKILL)
        assert line(p)["event"] == "unknown-delivered"
        assert p.wait(timeout=5) == 2
        assert "WorkerStopped" in failure(supervised)["value"]["reason"]
    finally:
        finish(p)


def test_same_executable_exec_ends_enrolled_lifetime(supervised):
    p = start(supervised)
    try:
        assert line(p) == {"event": "observing"}
        # Same PID and executable: only the kernel NOTE_EXEC boundary detects it.
        supervised["native"].stdin.write(b"x")
        supervised["native"].stdin.flush()
        assert line(p)["event"] == "unknown-delivered"
        assert p.wait(timeout=5) == 2
        assert "native process lifetime ended" in failure(supervised)["value"]["reason"]
        assert supervised["native"].poll() is None
    finally:
        finish(p)


def test_supervisor_sigkill_is_observed_by_worker(supervised):
    p = start(supervised)
    try:
        assert line(p) == {"event": "observing"}
        p.kill()
        assert line(p)["event"] == "unknown-delivered"
        assert "supervisor process lifetime ended" in failure(supervised)["value"]["reason"]
    finally:
        finish(p)


def test_joint_crash_restart_reports_unclosed_lifetime_before_native_drain(supervised):
    p = start(supervised)
    try:
        assert line(p) == {"event": "observing"}
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=5)
    finally:
        finish(p)
    append(supervised)
    again = start(supervised)
    try:
        assert line(again)["event"] == "unknown-delivered"
        assert again.wait(timeout=5) == 2
        failure(supervised)
        assert supervised["ledger"].delivery(supervised["registrations"]["native"], supervised["target"]) is None
    finally:
        finish(again)


def test_second_supervisor_refuses_without_stopping_first(supervised):
    p = start(supervised)
    other = None
    try:
        assert line(p) == {"event": "observing"}
        other = start(supervised)
        assert other.wait(timeout=5) == 2
        assert p.poll() is None
        assert not (supervised["sender"] / "failure.json").exists()
        append(supervised)
        assert line(p)["event"] == "ingested"
    finally:
        if other is not None:
            finish(other)
        finish(p)

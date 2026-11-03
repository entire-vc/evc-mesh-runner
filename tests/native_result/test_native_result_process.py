import copy
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src"
sys.path.insert(0, str(SCRIPTS))
from mesh_runner.native_result_ledger import Refused
from mesh_runner.native_result_process import snapshot, validate, verify


pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"),
                                reason="owned process fixture needs Darwin/Linux")
_PRIVATE_ARGUMENT = "fixture-private-argv-must-never-escape"
_CHILD = "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)"


@pytest.fixture
def child():
    process = subprocess.Popen([sys.executable, "-c", _CHILD, _PRIVATE_ARGUMENT],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.readline() == b"ready\n"
        yield process
    finally:
        # Only this fixture's own child is signalled, if graceful EOF fails.
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def _birth_change(identity):
    key = "tv_sec" if identity["platform"] == "darwin" else "start_ticks"
    identity["birth"][key] += 1


def test_owned_child_and_current_identity_are_exact_json(child, capsys):
    identity = snapshot(child.pid)
    assert identity["pid"] == child.pid
    assert identity["uid"] == os.getuid()
    assert identity["platform"] == sys.platform
    assert verify(json.loads(json.dumps(identity))) == identity
    assert verify(snapshot(os.getpid()))["pid"] == os.getpid()
    encoded = json.dumps(identity)
    assert _PRIVATE_ARGUMENT not in encoded
    assert sys.executable not in encoded
    assert set(identity) == {"platform", "pid", "uid", "birth", "executable_sha256"}
    assert capsys.readouterr() == ("", "")


def test_digest_is_actual_executable_bytes():
    # macOS Python may exec an application binary different from sys.executable.
    # Use an unambiguous real executable for the independent byte comparison.
    process = subprocess.Popen(["/bin/cat"], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        expected = hashlib.sha256()
        with open("/bin/cat", "rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                expected.update(block)
        assert snapshot(process.pid)["executable_sha256"] == expected.hexdigest()
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("changed", ["birth", "pid", "uid", "digest", "platform"])
def test_live_mismatches_refused(child, changed):
    identity = snapshot(child.pid)
    if changed == "birth":
        _birth_change(identity)
    elif changed == "pid":
        identity["pid"] = os.getpid()
    elif changed == "uid":
        identity["uid"] = (os.getuid() + 1) % 2**32
    elif changed == "digest":
        identity["executable_sha256"] = "0" * 64
    else:
        identity["platform"] = "linux" if sys.platform == "darwin" else "darwin"
        identity["birth"] = ({"start_ticks": 1,
                              "boot_id": "00000000-0000-0000-0000-000000000000"}
                             if identity["platform"] == "linux"
                             else {"tv_sec": 1, "tv_usec": 0})
    assert validate(identity) == identity  # Structural validation is portable.
    with pytest.raises(Refused):
        verify(identity)


def test_validate_returns_independent_copy_and_accepts_remote_identity(child):
    identity = snapshot(child.pid)
    identity["uid"] = (os.getuid() + 1) % 2**32
    result = validate(identity)
    _birth_change(result)
    assert result["birth"] != identity["birth"]


@pytest.mark.parametrize("pid", [None, True, False, 0, -1, 2**31, 1.0, "1", [], {}])
def test_invalid_pid_refused_without_os_reads(pid):
    with pytest.raises(Refused, match="invalid process PID"):
        snapshot(pid)


@pytest.mark.parametrize("field,bad", [
    ("pid", True), ("pid", 0), ("pid", 2**31), ("pid", "12"),
    ("uid", False), ("uid", -1), ("uid", 2**32),
    ("platform", "freebsd"), ("platform", []),
    ("executable_sha256", "a" * 63), ("executable_sha256", "A" * 64),
    ("executable_sha256", "a" * 64 + "\n"), ("executable_sha256", 1),
    ("birth", None), ("birth", []), ("birth", {"extra": 1}),
])
def test_strict_schema_rejects_malformed_fields(child, field, bad):
    identity = snapshot(child.pid)
    identity[field] = bad
    with pytest.raises(Refused):
        validate(identity)


@pytest.mark.parametrize("shape", [None, [], 1, "identity", {}, {"extra": 1}])
def test_strict_schema_rejects_wrong_outer_shape(shape):
    with pytest.raises(Refused):
        validate(shape)


@pytest.mark.parametrize("platform,birth", [
    ("darwin", {"tv_sec": 0, "tv_usec": 0}),
    ("darwin", {"tv_sec": 1, "tv_usec": 1000000}),
    ("darwin", {"tv_sec": 1, "tv_usec": True}),
    ("darwin", {"tv_sec": 1.0, "tv_usec": 0}),
    ("darwin", {"tv_sec": 1, "tv_usec": 0, "extra": 1}),
    ("linux", {"start_ticks": -1, "boot_id": "00000000-0000-0000-0000-000000000000"}),
    ("linux", {"start_ticks": True, "boot_id": "00000000-0000-0000-0000-000000000000"}),
    ("linux", {"start_ticks": 1, "boot_id": "not-a-boot-id"}),
    ("linux", {"start_ticks": 1, "boot_id": "AAAAAAAA-0000-0000-0000-000000000000"}),
])
def test_strict_birth_schema(platform, birth):
    with pytest.raises(Refused):
        validate({"platform": platform, "pid": 1, "uid": 1,
                  "birth": birth, "executable_sha256": "a" * 64})


def test_unknown_fields_refused(child):
    identity = snapshot(child.pid)
    identity["command"] = _PRIVATE_ARGUMENT
    with pytest.raises(Refused, match="invalid process identity fields"):
        validate(identity)


def test_exited_child_refused_without_leaking_path_or_arguments(child, capsys):
    identity = snapshot(child.pid)
    child.stdin.write(b"x")
    child.stdin.flush()
    child.wait(timeout=5)
    for operation in (lambda: snapshot(child.pid), lambda: verify(identity)):
        with pytest.raises(Refused) as error:
            operation()
        assert _PRIVATE_ARGUMENT not in str(error.value)
        assert sys.executable not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_zombie_child_is_refused_before_reaping(child):
    identity = snapshot(child.pid)
    queue = None
    try:
        if sys.platform == "darwin":
            queue = select.kqueue()
            queue.control([select.kevent(child.pid, filter=select.KQ_FILTER_PROC,
                                         flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                                         fflags=select.KQ_NOTE_EXIT)], 0, 0)
        child.stdin.write(b"x")
        child.stdin.flush()
        # Observe exit in the kernel without reaping; the fixture reaps it.
        if queue is not None:
            events = queue.control(None, 1, 5)
            assert len(events) == 1 and events[0].ident == child.pid
            assert events[0].fflags & select.KQ_NOTE_EXIT
        else:
            result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            assert result.si_pid == child.pid
    finally:
        if queue is not None:
            queue.close()
    with pytest.raises(Refused):
        snapshot(child.pid)
    with pytest.raises(Refused):
        verify(identity)


def test_real_restart_has_distinct_identity(child):
    before = snapshot(child.pid)
    child.stdin.write(b"x")
    child.stdin.flush()
    child.wait(timeout=5)
    replacement = subprocess.Popen([sys.executable, "-c", _CHILD], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert replacement.stdout.readline() == b"ready\n"
        after = snapshot(replacement.pid)
        assert before != after
        assert before["pid"] != after["pid"] or before["birth"] != after["birth"]
        assert verify(after) == after
        # Even a caller substituting a real replacement PID cannot re-use birth.
        stale = copy.deepcopy(before)
        stale["pid"] = after["pid"]
        if stale["birth"] == after["birth"]:
            # Linux start ticks can coincide for a rapid restart. The full
            # incarnation still differs by PID; make the stale-birth red
            # control explicit instead of assuming finer kernel precision.
            _birth_change(stale)
        with pytest.raises(Refused):
            verify(stale)
    finally:
        replacement.stdin.close()
        try:
            replacement.wait(timeout=5)
        except subprocess.TimeoutExpired:
            replacement.kill()
            replacement.wait(timeout=5)
        replacement.stdout.close()

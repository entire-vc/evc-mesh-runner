"""A process group whose only members are zombies is gone; any member that can still run keeps it alive.

The leader of a turn is the runner's own child: when it exits, it stays a zombie until the
runner reaps it. ``killpg(pgid, 0)`` cannot tell such a group from a live one (Linux answers
success, Darwin answers EPERM), so the turn looked alive, held its capacity reservation and was
never settled until the next tick reaped it."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mesh_runner.adapters import base
from mesh_runner.adapters.base import group_alive, proc_stamp

POSIX = sys.platform == "darwin" or sys.platform.startswith("linux")


def members(pgid: int) -> list[tuple[int, str]]:
    """(pid, state letter) of the group's members: the test's own view, independent of the code under
    test (``ps`` on macOS, ``/proc`` on Linux, whose image may ship no ``ps``)."""
    if sys.platform.startswith("linux"):
        rows = []
        for name in os.listdir("/proc"):
            try:
                with open(f"/proc/{name}/stat") as fh:
                    rest = fh.read().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if name.isdigit() and rest[2] == str(pgid):
                rows.append((int(name), rest[0]))
        return sorted(rows)
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True).stdout
    rows = [line.split() for line in out.splitlines()]
    return sorted((int(r[0]), r[2][:1]) for r in rows if len(r) >= 3 and r[1] == str(pgid))


def wait_for(cond, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


@unittest.skipUnless(POSIX, "kernel process state")
class ZombieGroup(unittest.TestCase):
    def spawn(self, code: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)

        def cleanup():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
        self.addCleanup(cleanup)
        return proc

    def test_an_unreaped_leader_alone_is_a_gone_group(self):
        proc = self.spawn("pass")
        stamp = proc_stamp(proc.pid)
        wait_for(lambda: members(proc.pid) == [(proc.pid, "Z")], "the leader to be a zombie")
        # the premise: the group still answers killpg, as a live group does (success or EPERM)
        try:
            os.killpg(proc.pid, 0)
        except PermissionError:
            pass
        self.assertFalse(group_alive(proc.pid, stamp))
        self.assertIsNone(proc.returncode)                          # nothing reaped it on the way
        self.assertEqual(proc.wait(timeout=10), 0)                  # its exit status is still ours
        self.assertFalse(group_alive(proc.pid, stamp))

    def test_a_running_leader_is_alive(self):
        proc = self.spawn("import time; time.sleep(30)")
        stamp = proc_stamp(proc.pid)
        wait_for(lambda: bool(members(proc.pid)), "the leader to start")
        self.assertTrue(group_alive(proc.pid, stamp))

    def test_a_live_child_keeps_the_group_of_a_zombie_leader_alive(self):
        proc = self.spawn("import subprocess, sys; "
                          "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                          "child.pid")
        wait_for(lambda: (proc.pid, "Z") in members(proc.pid) and len(members(proc.pid)) == 2,
                 "a zombie leader beside its live child")
        self.assertTrue(group_alive(proc.pid))                      # the straggler grace relies on it
        child = next(pid for pid, _ in members(proc.pid) if pid != proc.pid)
        os.kill(child, signal.SIGKILL)                              # the orphan is reaped by init
        wait_for(lambda: members(proc.pid) == [(proc.pid, "Z")], "the child to be gone")
        self.assertFalse(group_alive(proc.pid))

    def test_an_unreadable_state_keeps_a_zombie_group_alive(self):
        proc = self.spawn("pass")
        wait_for(lambda: members(proc.pid) == [(proc.pid, "Z")], "the leader to be a zombie")
        reader = "_darwin_procs" if sys.platform == "darwin" else "_linux_state"
        # mock: external boundary — the kernel's process table refuses the read
        with mock.patch.object(base, reader, return_value=None):
            self.assertTrue(group_alive(proc.pid))

    def test_another_users_group_stays_alive(self):
        # pid 1 (init / launchd) leads a group of root's live processes: EPERM, not a zombie
        self.assertTrue(group_alive(1))


class LinuxProcTable(unittest.TestCase):
    """The Linux reading of a group, over a fixture ``/proc`` (the kernel's table is the external boundary)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        (self.proc / "self").mkdir()                             # non-numeric entries are skipped

    def process(self, pid: int, state: str, pgrp: int, comm: str = "agent") -> None:
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "stat").write_text(f"{pid} ({comm}) {state} 1 {pgrp} {pgrp} 0 -1 4194304 0\n")

    def only_zombies(self, pgid: int) -> bool:
        with mock.patch.object(base.sys, "platform", "linux"):
            return base.only_zombies(pgid, proc_root=str(self.proc))

    def test_a_zombie_leader_alone_is_only_zombies(self):
        self.process(100, "Z", 100, comm="a (b) c")             # comm may hold spaces and parentheses
        self.process(200, "S", 200)
        self.assertTrue(self.only_zombies(100))

    def test_zombie_members_only_are_only_zombies(self):
        self.process(100, "Z", 100)
        self.process(101, "Z", 100)
        self.assertTrue(self.only_zombies(100))

    def test_a_running_member_keeps_the_group(self):
        self.process(100, "Z", 100)
        self.process(101, "S", 100, comm=") Z 1 100 100")      # a hostile comm does not fake the state
        self.assertFalse(self.only_zombies(100))

    def test_a_running_leader_keeps_the_group(self):
        self.process(100, "R", 100)
        self.assertFalse(self.only_zombies(100))

    def test_a_missing_or_unreadable_leader_keeps_the_group(self):
        self.assertFalse(self.only_zombies(100))
        (self.proc / "100").mkdir()
        (self.proc / "100" / "stat").write_text("garbage")
        self.assertFalse(self.only_zombies(100))


if __name__ == "__main__":
    unittest.main()

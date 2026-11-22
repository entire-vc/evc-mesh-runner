"""A git the Runner kills on its timeout must not leave its index.lock behind.

The killed process cannot clean up, so every later git write in that worktree failed with
"index.lock': File exists" until someone moved the lock by hand. Only a lock that this killed
git created (after its start, nobody holding it) is set aside; any other lock stays put."""
import ctypes
import ctypes.util
import errno
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mesh_runner import workspace
from mesh_runner.workspace import WorkspaceError, git
from test_workspace import make_repo

HOLD = ("import os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n")


def slot_with_slow_checkout(root: Path) -> Path:
    """A linked worktree (like a pool slot) whose checkout hangs in a smudge filter while git
    holds the worktree's index.lock."""
    base = make_repo(root)
    (base / ".gitattributes").write_text("*.slow filter=slow\n")
    (base / "f.slow").write_text("data\n")
    git(base, "add", ".")
    git(base, "commit", "-q", "-m", "slow file")
    slot = root / "slot-1"
    git(base, "worktree", "add", "-q", "--detach", str(slot))
    git(base, "config", "filter.slow.smudge", f"echo $$ > '{root / 'filter.pid'}'; exec sleep 10")
    (slot / "f.slow").unlink()
    return slot


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def gitdir_of(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--absolute-git-dir"))


def exchange_abi_failure(error, *, on_call=1):
    """An unavailable kernel primitive, or one failing on a specific call; otherwise real ABI."""
    if error is None:
        return SimpleNamespace()
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    symbol = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    native = getattr(libc, symbol)
    native.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    calls = 0

    def exchange(*args):
        nonlocal calls
        calls += 1
        if calls == on_call:
            ctypes.set_errno(error)
            return -1
        return native(*args)
    return SimpleNamespace(**{symbol: exchange})


class KilledGitLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_lock_of_a_git_killed_on_timeout_is_set_aside_and_next_reset_passes(self):
        slot = slot_with_slow_checkout(self.root)
        self.addCleanup(self._kill_filter)
        gd = gitdir_of(slot)
        logs = Captured()
        workspace.log.addHandler(logs)
        self.addCleanup(workspace.log.removeHandler, logs)
        with self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(slot, "checkout", "--quiet", "--force", "--detach", "HEAD", timeout=3)
        git(slot, "config", "--unset", "filter.slow.smudge")
        git(slot, "reset", "--hard", "--quiet")          # refused with "File exists" before the fix
        self.assertFalse(os.path.lexists(gd / "index.lock"))
        parked = [p.name for p in gd.iterdir() if p.name.startswith("index.lock.killed-")]
        self.assertEqual(len(parked), 1, sorted(p.name for p in gd.iterdir()))
        line = "\n".join(logs.lines)
        self.assertIn(str(gd / "index.lock"), line)
        self.assertIn(parked[0], line)

    def _kill_filter(self):
        """The smudge filter outlives the killed git (only git itself is killed); end it here."""
        pid = self.root / "filter.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass

    def _timeout_after(self, make_lock):
        """git() whose process times out; make_lock runs inside the attempt, after its start."""
        def run(argv, *, env, timeout):
            make_lock()
            raise subprocess.TimeoutExpired(argv, timeout)
        return patch("mesh_runner.workspace._run_git", side_effect=run)

    def test_control_lock_older_than_the_killed_git_stays(self):
        base = make_repo(self.root)
        lock = gitdir_of(base) / "index.lock"
        lock.write_text("")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with self._timeout_after(lambda: None), self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "reset", "--hard", "--quiet", timeout=5)
        self.assertTrue(lock.exists())
        self.assertEqual([p.name for p in lock.parent.iterdir() if ".killed-" in p.name], [])

    def test_control_lock_present_just_before_the_start_stays(self):
        """Boundary: a holderless lock that existed when git started is not ours, however close its
        timestamp is to the start (a clock slack must not admit it). Real git, killed on timeout."""
        base = make_repo(self.root)
        lock = gitdir_of(base) / "index.lock"
        lock.write_text("")
        st = os.lstat(lock)
        born = st.st_mtime_ns
        if getattr(st, "st_birthtime", None) is not None:
            born = min(born, int(st.st_birthtime * 1_000_000_000))
        with patch("mesh_runner.workspace.time.time_ns", return_value=born + 50_000_000), \
                self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!sleep 3", "stall", timeout=0.5)
        self.assertTrue(lock.exists())
        self.assertEqual([p.name for p in lock.parent.iterdir() if ".killed-" in p.name], [])

    def test_control_lock_with_a_live_holder_stays(self):
        base = make_repo(self.root)
        lock = gitdir_of(base) / "index.lock"
        holder = []

        def hold():
            p = subprocess.Popen([sys.executable, "-c", HOLD, str(lock)], stdout=subprocess.PIPE, text=True)
            holder.append(p)
            self.assertEqual(p.stdout.readline().strip(), "ready")

        def stop():
            for p in holder:
                p.kill()
                p.wait()
                p.stdout.close()
        self.addCleanup(stop)
        with self._timeout_after(hold), self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "reset", "--hard", "--quiet", timeout=5)
        self.assertTrue(lock.exists())
        self.assertEqual([p.name for p in lock.parent.iterdir() if ".killed-" in p.name], [])

    def test_positive_control_same_lock_without_holder_is_set_aside(self):
        """The harness of the live-holder control, minus the holder: the lock goes."""
        base = make_repo(self.root)
        lock = gitdir_of(base) / "index.lock"
        with self._timeout_after(lambda: lock.write_text("")), \
                self.assertRaisesRegex(WorkspaceError, "timed out"), self.assertLogs("mesh_runner", "WARNING"):
            git(base, "reset", "--hard", "--quiet", timeout=5)
        self.assertFalse(lock.exists())
        self.assertEqual(len([p for p in lock.parent.iterdir() if p.name.startswith("index.lock.killed-")]), 1)

    def test_control_replaced_lock_at_recovery_rename_keeps_live_holder(self):
        """A separate repair replaces the orphan after the final check, before rename.

        The new O_EXCL lock belongs to a live writer and must stay at index.lock.
        Only the OS boundary is synchronized; git, timeout, files and holder are real.
        """
        base = make_repo(self.root)
        gd = gitdir_of(base)
        lock = gd / "index.lock"
        real_open = os.open
        holders = []
        live_inode = []

        def stop():
            for p in holders:
                p.kill()
                p.wait()
                p.stdout.close()
        self.addCleanup(stop)

        def open_after_replacement(path, flags, *args, **kwargs):
            if path == gd and not holders:
                p = subprocess.Popen(
                    [sys.executable, "-c",
                     "import os, sys, time\n"
                     "lock = sys.argv[1]\n"
                     "os.rename(lock, lock + '.external-repair')\n"
                     "fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)\n"
                     "print(os.fstat(fd).st_ino, flush=True)\n"
                     "time.sleep(60)\n", str(lock)],
                    stdout=subprocess.PIPE, text=True)
                holders.append(p)
                live_inode.append(int(p.stdout.readline().strip()))
            return real_open(path, flags, *args, **kwargs)

        # mock: external boundary — force replacement between the final check and rename.
        with patch("mesh_runner.workspace.os.open", side_effect=open_after_replacement), \
                self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
        self.assertEqual(len(holders), 1, "race synchronization must run")
        self.assertIsNone(holders[0].poll(), "positive control: the new writer is alive")
        self.assertTrue(lock.exists(), "recovery moved the live writer's replacement lock")
        self.assertEqual(lock.stat().st_ino, live_inode[0])
        self.assertFalse(list(gd.glob("index.lock.killed-*")))

    def test_barrier_excludes_writer_and_cannot_replace_index(self):
        base = make_repo(self.root)
        gd = gitdir_of(base)
        lock = gd / "index.lock"
        original = (gd / "index").read_bytes()
        real_stat = os.stat
        observed = []

        def stat_during_exchange(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if (isinstance(path, str) and path.startswith("index.lock.killed-")
                    and kwargs.get("dir_fd") is not None and not observed and lock.is_dir()):
                # Both operations use real syscalls in a separate process while the barrier exists.
                result = subprocess.run(
                    [sys.executable, "-c",
                     "import errno, os, sys\n"
                     "lock, target = sys.argv[1:]\n"
                     "try: os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
                     "except OSError as e: assert e.errno == errno.EEXIST\n"
                     "else: raise AssertionError('second writer acquired the lock')\n"
                     "try: os.rename(lock, target)\n"
                     "except OSError as e: assert e.errno in (errno.ENOTDIR, errno.EISDIR)\n"
                     "else: raise AssertionError('barrier replaced the index')\n"
                     "print('writer excluded; index commit refused')\n", str(lock), str(gd / "index")],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed.append(result.stdout.strip())
            return st

        # mock: external boundary — inspect the real barrier before post-exchange stat returns.
        with patch("mesh_runner.workspace.os.stat", side_effect=stat_during_exchange), \
                self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
        self.assertEqual(observed, ["writer excluded; index commit refused"])
        self.assertEqual((gd / "index").read_bytes(), original)
        self.assertFalse(lock.exists())
        self.assertEqual(len(list(gd.glob("index.lock.killed-*"))), 1)

    def test_control_holder_opening_same_inode_before_exchange_is_returned(self):
        base = make_repo(self.root)
        gd = gitdir_of(base)
        lock = gd / "index.lock"
        real_open = os.open
        holders = []
        inode = []

        def stop():
            for p in holders:
                p.kill()
                p.wait()
                p.stdout.close()
        self.addCleanup(stop)

        def open_with_new_holder(path, flags, *args, **kwargs):
            if path == gd and not holders:
                p = subprocess.Popen(
                    [sys.executable, "-c",
                     "import os, sys, time\n"
                     "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                     "print(os.fstat(fd).st_ino, flush=True)\n"
                     "time.sleep(60)\n", str(lock)], stdout=subprocess.PIPE, text=True)
                holders.append(p)
                inode.append(int(p.stdout.readline().strip()))
            return real_open(path, flags, *args, **kwargs)

        # mock: external boundary — a real writer opens the same inode after the first holder check.
        with patch("mesh_runner.workspace.os.open", side_effect=open_with_new_holder), \
                self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
        self.assertEqual(len(holders), 1)
        self.assertIsNone(holders[0].poll())
        self.assertEqual(lock.stat().st_ino, inode[0])
        self.assertEqual(list(gd.glob("index.lock.killed-*")), [])

    def test_control_exchange_unavailable_preserves_lock(self):
        for error in (None, errno.ENOTSUP, errno.EINVAL):
            with self.subTest(error=error), tempfile.TemporaryDirectory(dir=self.root) as root:
                base = make_repo(Path(root))
                gd = gitdir_of(base)
                abi = exchange_abi_failure(error)
                # mock: external boundary — absent libc symbol or unsupported kernel exchange.
                with patch("ctypes.CDLL", return_value=abi), \
                        self.assertRaisesRegex(WorkspaceError, "timed out"):
                    git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
                self.assertTrue((gd / "index.lock").is_file())
                self.assertEqual(list(gd.glob("index.lock.killed-*")), [])

    def test_control_missing_index_preserves_lock(self):
        base = make_repo(self.root)
        gd = gitdir_of(base)
        (gd / "index").unlink()
        with self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
        self.assertTrue((gd / "index.lock").is_file())
        self.assertFalse((gd / "index").exists())
        self.assertEqual(list(gd.glob("index.lock.killed-*")), [])

    def test_failed_return_retains_barrier_and_logs_manual_recovery(self):
        base = make_repo(self.root)
        gd = gitdir_of(base)
        real_stat = os.stat
        changed = []

        def stat_with_uncertain_candidate(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if (isinstance(path, str) and path.startswith("index.lock.killed-")
                    and kwargs.get("dir_fd") is not None and not changed
                    and (gd / "index.lock").is_dir()):
                # Force a genuine change on the moved inode: the post-exchange check must reject it.
                os.utime(gd / path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
                changed.append(path)
                return real_stat(path, *args, **kwargs)
            return st

        abi = exchange_abi_failure(errno.EIO, on_call=2)
        # mock: external boundary — syscall failure during rollback, after a real first exchange.
        with patch("ctypes.CDLL", return_value=abi), \
                patch("mesh_runner.workspace.os.stat", side_effect=stat_with_uncertain_candidate), \
                self.assertLogs("mesh_runner", "ERROR") as logs, \
                self.assertRaisesRegex(WorkspaceError, "timed out"):
            git(base, "-c", "alias.stall=!touch .git/index.lock; exec sleep 3", "stall", timeout=0.5)
        self.assertEqual(len(changed), 1, "post-exchange change must have happened")
        self.assertTrue((gd / "index.lock").is_dir())
        self.assertTrue((gd / changed[0]).is_file())
        self.assertIn("barrier retained", "\n".join(logs.output))
        self.assertIn(f"rmdir -- {gd / 'index.lock'} && mv -n -- {gd / changed[0]}", "\n".join(logs.output))
        with self.assertRaisesRegex(WorkspaceError, "index.lock"):
            git(base, "reset", "--hard", "--quiet")

    def test_git_dir_argument_names_the_gitdir(self):
        """workspace.py runs git with --git-dir=<common>: the lock is looked for there."""
        base = make_repo(self.root)
        common = gitdir_of(base)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        lock = common / "HEAD.lock"
        with self._timeout_after(lambda: lock.write_text("")), \
                self.assertRaisesRegex(WorkspaceError, "timed out"), self.assertLogs("mesh_runner", "WARNING"):
            git(elsewhere, f"--git-dir={common}", f"--work-tree={base}", "reset", "--hard", timeout=5)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()

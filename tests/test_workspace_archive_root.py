import errno
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, _copy_file_fd, git
from tests.test_workspace import make_repo

def ws_digest():
    from mesh_runner.workspace import _entry_digest
    return _entry_digest


EXTERNAL = Path("/Volumes/fleet")          # a real second device on the Mini; tests needing it skip elsewhere


def _external_ok() -> bool:
    return EXTERNAL.is_dir() and os.path.ismount(EXTERNAL) and os.access(EXTERNAL, os.W_OK) \
        and os.stat(EXTERNAL).st_dev != os.stat(tempfile.gettempdir()).st_dev


class ArchiveRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / "s.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def pool(self, **kw):
        return WorkspacePool(self.store, self.root / "state" / "worktrees",
                             [RepoSpec("app", self.base, max_slots=2)],
                             identity=("Agent", "agent@example.com"), owner="agent", **kw)

    def scratch(self, pool):
        a = pool.lease("a1", "app")
        (a / "evidence").mkdir()
        (a / "evidence" / "x.txt").write_bytes(b"proof\x00\xff")
        os.symlink("/nonexistent", a / "dang")
        return a

    def test_configured_root_without_mount_requirement_is_used(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = self.scratch(pool)
        self.assertEqual(pool.release("a1"), [])
        found = list(arch.glob("agent/*/evidence/x.txt"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].read_bytes(), b"proof\x00\xff")
        self.assertFalse((self.root / "state" / "slot-archive").exists())
        self.assertEqual(pool.lease("a2", "app"), a)

    def test_unmounted_required_volume_holds_slot_and_keeps_source_and_creates_nothing(self):
        arch = self.root / "missing-ssd" / "archive"            # not a mount point, does not exist
        pool = self.pool(archive_root=arch, archive_require_mount=self.root / "missing-ssd")
        a = self.scratch(pool)
        held = pool.release("a1")
        self.assertEqual(len(held), 1)
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")
        self.assertFalse((self.root / "missing-ssd").exists())
        self.assertNotEqual(pool.lease("a2", "app"), a)          # slot not freed

    def test_archive_on_internal_device_is_refused(self):
        arch = self.root / "arch"
        arch.mkdir()
        # a real mount point that is the internal device: "/" is always a mount
        pool = self.pool(archive_root=arch, archive_require_mount=Path("/"))
        a = self.scratch(pool)
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertTrue((a / "evidence" / "x.txt").exists())
        self.assertEqual(list(arch.glob("**/x.txt")), [])

    def test_write_error_midway_keeps_every_source_file_and_holds_slot(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "a-ok.txt").write_text("one")
        (a / "z-bad.txt").write_text("two")
        real_copy = _copy_file_fd

        def full_disk(src, dfd, name):         # the archive volume fills up on the second file (root-safe)
            if str(src).endswith("z-bad.txt"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copy(src, dfd, name)

        with mock.patch("mesh_runner.workspace._copy_file_fd", full_disk):
            held = pool.release("a1")
        self.assertEqual(len(held), 1)
        self.assertEqual((a / "a-ok.txt").read_text(), "one")
        self.assertEqual((a / "z-bad.txt").read_text(), "two")

    def test_incomplete_archived_copy_keeps_the_source(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "f.txt").write_bytes(b"0123456789")
        real_copy = _copy_file_fd

        def truncating(src, dfd, name):         # a copy that "succeeds" but writes short
            real_copy(src, dfd, name)
            fd = os.open(name, os.O_WRONLY | os.O_TRUNC, dir_fd=dfd)
            os.write(fd, b"0123")
            os.close(fd)

        with mock.patch("mesh_runner.workspace._copy_file_fd", truncating):
            self.assertEqual(len(pool.release("a1")), 1)
        self.assertEqual((a / "f.txt").read_bytes(), b"0123456789")

    def test_unconfigured_pool_keeps_legacy_location(self):
        pool = self.pool()
        self.scratch(pool)
        pool.release("a1")
        self.assertEqual(len(list((self.root / "state").glob("slot-archive/agent/*/evidence/x.txt"))), 1)

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_internal_logical_root_with_external_symlinked_slot_archives_to_external(self):
        ext = Path(tempfile.mkdtemp(prefix="archive-root-test-", dir=EXTERNAL))
        try:
            pool = self.pool(archive_root=ext / "archive", archive_require_mount=EXTERNAL)
            (ext / "archive").mkdir()
            a = pool.lease("a1", "app")            # logical path stays on the internal pool root
            real = ext / "slot"
            shutil.move(str(a), str(real))        # move the slot to the SSD, leave a symlink behind
            os.symlink(real, a)
            git(self.base, "worktree", "repair", str(real))
            (real / "evidence").mkdir()
            (real / "evidence" / "x.txt").write_bytes(b"proof\x00\xff")
            self.assertEqual(os.stat(a).st_dev, os.stat(real).st_dev)
            self.assertNotEqual(os.stat(a).st_dev, os.stat(self.root).st_dev)
            pool._archive_untracked(a)
            found = list((ext / "archive").glob("agent/*/evidence/x.txt"))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].read_bytes(), b"proof\x00\xff")
            self.assertFalse((real / "evidence").exists())
        finally:
            shutil.rmtree(ext, ignore_errors=True)

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_external_volume_with_archive_on_internal_disk_is_refused(self):
        arch = self.root / "arch"                 # internal; mount requirement says the SSD
        arch.mkdir()
        pool = self.pool(archive_root=arch, archive_require_mount=EXTERNAL)
        a = self.scratch(pool)
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertTrue((a / "evidence" / "x.txt").exists())

    def _race_setup(self):
        ext = Path(tempfile.mkdtemp(prefix="archive-root-race-", dir=EXTERNAL))
        self.addCleanup(shutil.rmtree, ext, True)
        (ext / "archive").mkdir()
        decoy = self.root / "decoy"                 # stands for the internal mountpoint directory
        decoy.mkdir()
        pool = self.pool(archive_root=ext / "archive", archive_require_mount=EXTERNAL)
        a = pool.lease("a1", "app")
        (a / "evidence").mkdir()
        (a / "evidence" / "x.txt").write_bytes(b"proof\x00\xff")
        (a / "sub" / "deep").mkdir(parents=True)
        (a / "sub" / "deep" / "f.txt").write_bytes(b"nested")
        return ext, decoy, pool, a

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_archive_root_substituted_after_the_check_does_not_redirect_writes(self):
        ext, decoy, pool, a = self._race_setup()

        def swap():                                 # path now resolves to the internal decoy
            os.rename(ext / "archive", ext / "archive-moved")
            os.symlink(decoy, ext / "archive")

        pool._archive_hook = swap
        with self.assertRaises(WorkspaceError):                             # the configured root no longer names our fd
            pool._archive_untracked(a)
        self.assertEqual(list(decoy.rglob("*")), [])                       # sentinel untouched
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")   # sources stay in the slot
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")

    @staticmethod
    def _unmounted_view():
        """After an unmount the mount path resolves to the internal directory under it: os.stat of the
        mount path answers for its parent (a different (dev, ino), same device as the parent)."""
        real_stat = os.stat

        def stat(path, *a, **kw):
            if kw.get("dir_fd") is None and str(path) == str(EXTERNAL):
                return real_stat(EXTERNAL.parent, *a, **kw)
            return real_stat(path, *a, **kw)
        return stat

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_mount_detached_before_a_rename_refuses_and_keeps_the_sources(self):
        ext, decoy, pool, a = self._race_setup()
        pool._pre_rename_hook = lambda: mock.patch("mesh_runner.workspace.os.stat", self._unmounted_view()).start()
        self.addCleanup(mock.patch.stopall)
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        mock.patch.stopall()
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")
        self.assertEqual(self._quarantined(a, "evidence/x.txt"), [])

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_mount_detached_before_complete_returns_every_entry_and_never_completes(self):
        ext, decoy, pool, a = self._race_setup()
        real_fwalk = os.fwalk
        self.addCleanup(mock.patch.stopall)

        def detach_then_walk(*args, **kw):          # after the last rename, before the complete receipt
            mock.patch("mesh_runner.workspace.os.stat", self._unmounted_view()).start()
            return real_fwalk(*args, **kw)
        with mock.patch("mesh_runner.workspace.os.fwalk", detach_then_walk), self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        mock.patch.stopall()
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")
        self.assertNotIn("complete", [d["state"] for _, d in pool._quarantine_runs(a)])

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_destination_lost_after_the_check_refuses_and_keeps_sources(self):
        ext, decoy, pool, a = self._race_setup()

        def lose():                                 # destination removed, its path replaced by the decoy
            for d in (ext / "archive").glob("agent/*"):
                shutil.rmtree(d)
                os.symlink(decoy, d)

        pool._archive_hook = lose
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertEqual(list(decoy.rglob("*")), [])
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_destination_lost_after_verification_keeps_all_sources(self):
        ext, decoy, pool, a = self._race_setup()

        def lose():                                 # archive gone after the copies were verified
            for d in (ext / "archive").glob("agent/*"):
                shutil.rmtree(d)
                os.symlink(decoy, d)

        pool._verified_hook = lose
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertEqual(list(decoy.rglob("*")), [])
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")

    def _quarantined(self, slot, rel):
        real = Path(os.path.realpath(slot))
        return list(real.parent.glob(f".quarantine-{real.name}/*/{rel}"))

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_archive_lost_after_last_read_back_before_rename_never_loses_source_bytes(self):
        ext, decoy, pool, a = self._race_setup()

        def lose():                                 # strictly after the last read-back, before the rename
            for d in (ext / "archive").glob("agent/*"):
                if not d.is_symlink():
                    shutil.rmtree(d)
                    os.symlink(decoy, d)

        pool._pre_rename_hook = lose
        try:
            pool._archive_untracked(a)
        except WorkspaceError:
            pass                                    # a later entry noticed the loss: slot held
        self.assertEqual(list(decoy.rglob("*")), [])                       # Data-decoy stays empty
        for rel, want in (("evidence/x.txt", b"proof\x00\xff"), ("sub/deep/f.txt", b"nested")):
            where = [p for p in [a / rel, *self._quarantined(a, rel)] if p.is_file()]
            self.assertEqual([p.read_bytes() for p in where][:1], [want], rel)   # bytes survive somewhere

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_archive_lost_after_the_renames_returns_every_entry_and_never_completes(self):
        ext, decoy, pool, a = self._race_setup()
        real_fwalk = os.fwalk

        def lose_then_walk(*args, **kw):            # after the last rename, before the complete receipt
            for d in (ext / "archive").glob("agent/*"):
                if not d.is_symlink():
                    shutil.rmtree(d)
                    os.symlink(decoy, d)
            return real_fwalk(*args, **kw)
        with mock.patch("mesh_runner.workspace.os.fwalk", lose_then_walk), self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertEqual(list(decoy.rglob("*")), [])
        self.assertEqual((a / "evidence" / "x.txt").read_bytes(), b"proof\x00\xff")       # back in the slot
        self.assertEqual((a / "sub" / "deep" / "f.txt").read_bytes(), b"nested")
        states = [d["state"] for _, d in pool._quarantine_runs(a)]
        self.assertNotIn("complete", states)

    @unittest.skipUnless(_external_ok(), "no writable external volume")
    def test_single_entry_archive_lost_in_the_gap_before_its_rename_is_not_renamed(self):
        ext, decoy, pool, a = self._race_setup()
        for p in (a / "evidence", a / "sub"):
            shutil.rmtree(p)
        (a / "only.txt").write_text("single")

        def lose():
            for d in (ext / "archive").glob("agent/*"):
                if not d.is_symlink():
                    shutil.rmtree(d)
                    os.symlink(decoy, d)
        pool._pre_rename_hook = lose
        with self.assertRaises(WorkspaceError):
            pool._archive_untracked(a)
        self.assertEqual((a / "only.txt").read_text(), "single")                # still in the slot
        self.assertEqual(self._quarantined(a, "only.txt"), [])

    def test_unrenamable_entry_retried_by_every_sweep_leaves_no_archive_copies(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "proof").mkdir()
        (a / "proof" / "f.txt").write_text("root-owned in real life")
        real_rename = os.rename

        def denied(src, dst, *args, **kw):              # root-safe stand-in for an entry that cannot be moved
            if src == "proof":
                raise PermissionError(errno.EACCES, "Permission denied", src)
            return real_rename(src, dst, *args, **kw)

        with mock.patch("mesh_runner.workspace.os.rename", denied):
            for _ in range(4):                          # four sweeps over the same held slot
                with self.assertRaises(WorkspaceError):
                    pool._archive_untracked(a)
        self.assertLessEqual(len(list((arch / "agent").glob("*"))), 1)
        self.assertEqual((a / "proof" / "f.txt").read_text(), "root-owned in real life")   # source intact
        with mock.patch("mesh_runner.workspace.os.rename", denied):
            self.assertEqual(len(pool.release("a1")), 1)                                   # slot held
        self.assertLessEqual(len(list((arch / "agent").glob("*"))), 1)
        real = Path(os.path.realpath(a))
        self.assertEqual(list(real.parent.glob(f".quarantine-{real.name}/*")), [])      # no empty quarantine runs

    def test_failed_archive_with_read_only_nested_dir_leaves_no_copy(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "ro" / "sub").mkdir(parents=True)
        (a / "ro" / "sub" / "f.txt").write_text("x")
        (a / "ro" / "g.txt").write_text("y")
        (a / "proof").mkdir()
        (a / "proof" / "f.txt").write_text("unmovable")
        os.chmod(a / "ro" / "sub", 0o555)
        os.chmod(a / "ro", 0o555)
        real_rename = os.rename

        def denied(src, dst, *args, **kw):
            if src == "proof":
                raise PermissionError(errno.EACCES, "Permission denied", src)
            return real_rename(src, dst, *args, **kw)

        try:
            with mock.patch("mesh_runner.workspace.os.rename", denied):
                with self.assertRaises(WorkspaceError):
                    pool._archive_untracked(a)
            self.assertEqual(list((arch / "agent").glob("*")) if (arch / "agent").exists() else [], [])
        finally:
            os.chmod(a / "ro", 0o755)
            os.chmod(a / "ro" / "sub", 0o755)

    def test_failed_run_dir_fsync_leaves_no_empty_quarantine_run(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "only.txt").write_text("single")
        real_fsync = os.fsync
        calls = {"n": 0}

        def flaky(fd):                                  # first fsync = the run directory entry sync
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EIO, "I/O error")
            return real_fsync(fd)

        with mock.patch("mesh_runner.workspace.os.fsync", flaky):
            with self.assertRaises(Exception):
                pool._archive_untracked(a)
        real = Path(os.path.realpath(a))
        self.assertEqual((a / "only.txt").read_text(), "single")
        self.assertEqual(list(real.parent.glob(f".quarantine-{real.name}/*")), [])

    def test_partial_rename_then_restart_and_slot_reuse_keep_the_quarantine(self):
        arch = self.root / "arch"
        arch.mkdir()
        pool = self.pool(archive_root=arch)
        a = pool.lease("a1", "app")
        (a / "a-one.txt").write_text("one")
        (a / "b-two.txt").write_text("two")
        real_rename, calls = os.rename, []

        def flaky(*args, **kw):
            calls.append(args)
            if len(calls) == 3:                 # 1: manifest commit, 2: first entry, 3: second entry
                raise OSError(errno.EIO, "boom")
            return real_rename(*args, **kw)

        with mock.patch("mesh_runner.workspace.os.rename", flaky):
            self.assertEqual(len(pool.release("a1")), 1)            # slot held
        self.assertEqual((a / "b-two.txt").read_text(), "two")      # not yet moved: still in the slot
        first = self._quarantined(a, "a-one.txt")
        self.assertEqual([p.read_text() for p in first], ["one"])
        manifest = json.loads((first[0].parent / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "renaming")
        self.assertEqual(sorted(e["path"][0] for e in manifest["entries"]), ["a-one.txt", "b-two.txt"])
        self.assertEqual(pool.recheck_held(open_attempts=set()), [str(a)])   # restart: rest archived, slot freed
        self.assertFalse((a / "b-two.txt").exists())
        self.assertEqual([p.read_text() for p in self._quarantined(a, "a-one.txt")], ["one"])
        self.assertEqual([p.read_text() for p in self._quarantined(a, "b-two.txt")], ["two"])
        self.assertEqual(pool.lease("a2", "app"), a)                # slot reused ...
        self.assertEqual(len(self._quarantined(a, "a-one.txt")), 1)  # ... quarantine untouched
        self.assertEqual(len(list(arch.glob("agent/*/a-one.txt"))), 1)
        self.assertGreaterEqual(len(list(arch.glob("agent/*/b-two.txt"))), 1)   # the failed run copied it too

    def test_quarantine_is_not_inside_the_slot_so_it_is_never_rearchived(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        self.assertEqual(pool.release("a1"), [])
        self.assertEqual(os.listdir(a).count(".quarantine-slot-1"), 0)
        self.assertEqual(len(self._quarantined(a, "evidence/x.txt")), 1)

    def test_quarantine_creation_failure_holds_the_slot_and_keeps_sources(self):
        pool = self.pool()
        a = pool.lease("a1", "app")
        (a / "f.txt").write_text("keep")
        (Path(os.path.realpath(a)).parent / ".quarantine-slot-1").write_text("not a directory")
        self.assertEqual(len(pool.release("a1")), 1)
        self.assertEqual((a / "f.txt").read_text(), "keep")

    def test_manifest_inventory_and_restore_round_trip(self):
        pool = self.pool()
        a = pool.lease("a1", "app")
        (a / "d").mkdir()
        (a / "d" / "f.bin").write_bytes(b"\x00\x01odd")
        os.symlink("../target", a / "ln")
        self.assertEqual(pool.release("a1"), [])
        inv = pool.quarantine_inventory(a, verify=True)
        self.assertEqual([r["state"] for r in inv], ["complete"])
        self.assertEqual(inv[0]["attempt"], "a1")
        self.assertTrue(inv[0]["archive"]["dir"])
        self.assertEqual({(e["path"], e["where"], e.get("intact")) for e in inv[0]["entries"]},
                         {("d", "quarantine", True), ("ln", "quarantine", True)})
        run = Path(inv[0]["run"]).name
        # occupied target refuses the whole restore, nothing moves
        (a / "ln").write_text("squatter")
        with self.assertRaises(WorkspaceError):
            pool.quarantine_restore(a, run)
        self.assertTrue((a / "ln").is_file() and not os.path.islink(a / "ln"))
        os.unlink(a / "ln")
        self.assertEqual(sorted(pool.quarantine_restore(a, run)), ["d", "ln"])
        self.assertEqual((a / "d" / "f.bin").read_bytes(), b"\x00\x01odd")
        self.assertEqual(os.readlink(a / "ln"), "../target")
        self.assertEqual(pool.quarantine_inventory(a)[0]["state"], "restored")

    def test_entry_missing_from_both_slot_and_quarantine_holds_the_slot(self):
        pool = self.pool()
        a = pool.lease("a1", "app")
        (a / "a-one.txt").write_text("one")
        (a / "b-two.txt").write_text("two")
        real_rename, calls = os.rename, []

        def flaky(*args, **kw):
            calls.append(args)
            if len(calls) == 3:
                raise OSError(errno.EIO, "boom")
            return real_rename(*args, **kw)

        with mock.patch("mesh_runner.workspace.os.rename", flaky):
            pool.release("a1")
        os.unlink(a / "b-two.txt")                  # lost out of band: in neither place
        self.assertEqual(pool.recheck_held(open_attempts=set()), [])      # no recovery receipt: stays held
        self.assertEqual(len(list(pool._quarantine_runs(a))), 1)

    def test_tampered_manifest_paths_are_rejected_before_anything_moves(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a))
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        for bad in (["..", "outside.txt"], ["/abs"], [], ["a/b"], [""], ["."]):
            doc = json.loads((run / "manifest.json").read_text())
            doc["entries"][0]["path"] = bad
            (run / "manifest.json").write_text(json.dumps(doc))
            with self.assertRaises(WorkspaceError):
                pool.quarantine_restore(a, run.name)
            self.assertEqual(pool.quarantine_inventory(a)[0]["state"], "unreadable")   # the CLI turns this into exit 1
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((a / "evidence").exists())            # nothing was moved back

    def test_short_manifest_write_is_completed(self):
        pool = self.pool()
        real_write = os.write

        def short(fd, data):                    # at most 7 bytes per call
            return real_write(fd, bytes(data)[:7])

        a = pool.lease("a1", "app")
        (a / "f.txt").write_text("x")
        with mock.patch("mesh_runner.workspace.os.write", short):
            self.assertEqual(pool.release("a1"), [])
        inv = pool.quarantine_inventory(a, verify=True)
        self.assertEqual([r["state"] for r in inv], ["complete"])
        self.assertEqual([e["intact"] for e in inv[0]["entries"]], [True])

    def test_restore_refuses_when_an_entry_is_neither_quarantined_nor_intact_in_the_slot(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a))
        shutil.rmtree(run / "evidence")                         # one entry lost from the quarantine
        with self.assertRaises(WorkspaceError):
            pool.quarantine_restore(a, run.name)
        self.assertFalse(os.path.lexists(a / "dang"))           # nothing was moved back
        self.assertEqual(json.loads((run / "manifest.json").read_text())["state"], "complete")
        (a / "evidence").mkdir()                                # present in the slot but not matching
        (a / "evidence" / "x.txt").write_text("other")
        with self.assertRaises(WorkspaceError):
            pool.quarantine_restore(a, run.name)

    def test_restore_never_replaces_an_entry_created_after_the_preflight_check(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a))
        real_digest, calls = ws_digest(), []

        def racing(path):                                       # a writer takes the target after the last check
            calls.append(path)
            if len(calls) == 3:
                (a / "dang").write_text("late writer")
            return real_digest(path)
        with mock.patch("mesh_runner.workspace._entry_digest", racing), self.assertRaises(WorkspaceError):
            pool.quarantine_restore(a, run.name)
        self.assertEqual((a / "dang").read_text(), "late writer")
        self.assertTrue(os.path.islink(run / "dang"))           # the quarantined copy is still there

    def test_cli_quarantine_commands_report_workspace_errors_without_traceback(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from mesh_runner import cli
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        runner = SimpleNamespace(agents=[SimpleNamespace(pool=pool)])
        for argv in (["quarantine-list", "--path", str(a) + "/../x"],
                     ["quarantine-restore", "--path", str(a), "--run", "no-such-run"]):
            err = io.StringIO()
            with mock.patch.object(cli.config_mod, "load", return_value=object()), \
                    mock.patch.object(cli, "Runner", return_value=runner), contextlib.redirect_stderr(err):
                rc = cli.main(["--config", "unused.toml", *argv])
            self.assertEqual(rc, 1)
            self.assertIn(argv[0], err.getvalue())

    def test_malformed_manifest_structure_holds_the_slot_and_the_cli_reports_it(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a))
        for bad in ('{"state": "renaming"}', '{"state": "renaming", "entries": 5}', '[]',
                    '{"state": "renaming", "entries": [{"path": "x"}]}', '{"state": "renaming", "entries": [3]}'):
            (run / "manifest.json").write_text(bad)
            with self.assertRaises(WorkspaceError):
                pool._reconcile_quarantine(a)
            with self.assertRaises(WorkspaceError):
                pool.quarantine_restore(a, run.name)
            self.assertEqual(pool.quarantine_inventory(a)[0]["state"], "unreadable")

    def test_cli_quarantine_list_exits_nonzero_on_a_malformed_manifest(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from mesh_runner import cli
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a))
        runner = SimpleNamespace(agents=[SimpleNamespace(pool=pool)])

        def call():
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli.config_mod, "load", return_value=object()), \
                    mock.patch.object(cli, "Runner", return_value=runner), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return cli.main(["--config", "unused.toml", "quarantine-list", "--path", str(a)]), out.getvalue(), err.getvalue()
        self.assertEqual(call()[0], 0)                          # healthy manifest: success
        good = json.loads((run / "manifest.json").read_text())
        e0 = good["entries"][0]
        variants = [{**good, "state": "bogus"}, {**good, "slot": 5}, {**good, "archive": {"root": "x"}},
                    {**good, "entries": [{**e0, "digest": None}]},
                    {**good, "entries": [{**e0, "digest": {"t": "file", "sha256": "zz", "size": 1}}]},
                    {**good, "entries": [{**e0, "digest": {"t": "mystery"}}]},
                    {**good, "entries": [{**e0, "path": ["..", "x"]}]}]
        for bad in ['{"state": "complete"}', '{"state": "complete", "entries": [3]}', "not json",
                    *map(json.dumps, variants)]:
            (run / "manifest.json").write_text(bad)
            rc, out, err = call()
            self.assertEqual(rc, 1)
            self.assertIn("unreadable", out)                    # the inventory is still shown
            self.assertIn("quarantine-list", err)

    def test_quarantine_directory_entries_are_fsynced_before_the_complete_receipt(self):
        pool = self.pool()
        a = self.scratch(pool)
        real = Path(os.path.realpath(a))
        qdir = real.parent / f".quarantine-{real.name}"
        names = {"slot": real, "pool dir": real.parent, "quarantine dir": qdir}
        seen, real_fsync = set(), os.fsync

        def spy(fd):
            ino = os.fstat(fd).st_ino
            runs = list(qdir.glob("*/manifest.json"))
            done = bool(runs) and json.loads(runs[0].read_text())["state"] == "complete"
            if not done:
                seen.update(n for n, p in names.items() if p.exists() and os.stat(p).st_ino == ino)
            return real_fsync(fd)
        with mock.patch("mesh_runner.workspace.os.fsync", spy):
            self.assertEqual(pool.release("a1"), [])
        self.assertEqual(seen, set(names))

    def test_restore_only_touches_a_held_or_free_slot_and_gives_the_state_back(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a)).name
        state = lambda: self.store.db.execute("SELECT state FROM slots WHERE path=?", (str(a),)).fetchone()[0]
        self.assertEqual(state(), "free")
        self.store.db.execute("UPDATE slots SET state='leased', attempt='a9' WHERE path=?", (str(a),))
        with self.assertRaises(WorkspaceError):                 # an agent is writing there
            pool.quarantine_restore(a, run)
        self.assertFalse(os.path.lexists(a / "dang"))           # nothing was injected
        self.assertEqual(state(), "leased")
        self.store.db.execute("UPDATE slots SET state='held', attempt='a9' WHERE path=?", (str(a),))
        real_restore, seen = pool._restore_run, []

        def spy(path, name):
            seen.append(state())                                # the row is claimed while files move
            return real_restore(path, name)
        with mock.patch.object(pool, "_restore_run", spy):
            self.assertEqual(sorted(pool.quarantine_restore(a, run)), ["dang", "evidence"])
        self.assertEqual(seen, ["freeing"])
        self.assertEqual(state(), "held")                       # and handed back as it was
        self.store.db.execute("UPDATE slots SET state='free', attempt='' WHERE path=?", (str(a),))
        self.assertEqual(pool.quarantine_restore(a, run), [])   # free slot: the run is already restored
        self.assertEqual(state(), "free")

    def test_quarantine_commands_refuse_traversal_and_unknown_paths(self):
        pool = self.pool()
        a = self.scratch(pool)
        pool.release("a1")
        run = next(r for r, _ in pool._quarantine_runs(a)).name
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for bad in (str(a) + "/../../../elsewhere", str(a.parent / "slot-1" / ".." / "slot-1"),
                    str(elsewhere), "relative/slot-1", str(a) + "-other"):
            with self.assertRaises(WorkspaceError, msg=bad):
                pool.quarantine_inventory(bad)
            with self.assertRaises(WorkspaceError, msg=bad):
                pool.quarantine_restore(bad, run)
        self.assertEqual(len(pool.quarantine_inventory(a)), 1)
        self.assertFalse((a / "evidence").exists())


"""Real filesystem permission holds survive restart and wake only on path repair."""
import os
import pwd
import json
import subprocess
import sys
import unittest

from fakes import FakeMesh, task
from test_runner import Bench


@unittest.skipIf(os.geteuid() == 0, "EACCES requires an unprivileged user")
class PermissionHold(Bench):
    def test_denied_resume_stops_until_exact_path_permissions_change(self):
        t = task(1)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="ready")
        folder = self.root / "blocked"
        folder.mkdir()
        target = folder / "proof"
        target.write_text("preserved work")
        folder.chmod(0o000)
        self.addCleanup(lambda: folder.chmod(0o700) if folder.exists() else None)
        invoked = []

        def restore(attempt):
            invoked.append(attempt.id)
            self.assertEqual(target.read_text(), "preserved work")

        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 1)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertFalse(r._quarantine)
        self.assertFalse(mesh.comment_log)
        self.assertFalse(mesh.checkouts)
        r = self.runner(mesh)
        for _ in range(3):
            r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 1)
        # Unrelated task and filesystem activity are not repair events.
        (self.root / "unrelated").write_text("activity")
        mesh.tasks[t["id"]]["updated_at"] = "2099-01-01T00:00:00Z"
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 1)
        folder.chmod(0o700)
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 2)
        events = [row[1] for row in r.store.events(a.id)]
        self.assertEqual(events.count("workspace_permission_hold"), 1)
        self.assertEqual(events.count("permission_path_changed"), 1)
        self.assertFalse(any(e.startswith("quarantine:") for e in events))

    def test_transient_os_error_keeps_existing_error_boundary(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")

        def transient(attempt):
            raise OSError(5, "I/O error", str(self.root / "other"))

        r._guard(a, "resume", transient)
        self.assertEqual(r._errors[(a.id, "resume")], 1)
        self.assertIsNone(r.store.mark(a.agent, a.task_id, "permission_hold:resume"))

    def test_symlink_target_repair_wakes_without_link_change(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")
        folder = self.root / "private"
        folder.mkdir()
        target = folder / "proof"
        target.write_text("preserved")
        link = self.root / "pointer"
        link.symlink_to(folder, target_is_directory=True)
        before = link.lstat()
        folder.chmod(0o000)
        self.addCleanup(lambda: folder.chmod(0o700) if folder.exists() else None)
        invoked = []

        def restore(attempt):
            invoked.append(attempt.id)
            self.assertEqual([p.name for p in link.iterdir()], ["proof"])

        r._guard(a, "resume", restore)
        r = self.runner(r.mesh)
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 1)
        folder.chmod(0o700)
        self.assertEqual(link.lstat(), before)
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 2)

    @unittest.skipUnless(sys.platform == "darwin", "real Darwin ACL")
    def test_acl_repair_wakes_without_posix_mode_change(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")
        target = self.root / "acl-proof"
        target.write_text("preserved")
        mode = target.stat().st_mode
        subprocess.run(["/bin/chmod", "+a", f"user:{pwd.getpwuid(os.geteuid()).pw_name} deny read", str(target)], check=True)
        self.addCleanup(lambda: subprocess.run(["/bin/chmod", "-N", str(target)], check=True) if target.exists() else None)
        invoked = []

        def restore(attempt):
            invoked.append(attempt.id)
            self.assertEqual(target.read_text(), "preserved")

        r._guard(a, "resume", restore)
        r = self.runner(r.mesh)
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 1)
        subprocess.run(["/bin/chmod", "-N", str(target)], check=True)
        self.assertEqual(target.stat().st_mode, mode)
        r._guard(a, "resume", restore)
        self.assertEqual(len(invoked), 2)

    def test_corrupt_hold_does_not_escape_error_boundary(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")
        for value in ("bad JSON", "[]", '{"attempt": "legacy"}',
                      json.dumps({"attempt": a.id, "path": "\x00", "signature": []})):
            with self.subTest(value=value):
                r.store.mark(a.agent, a.task_id, "permission_hold:resume", value)
                invoked = []
                r._guard(a, "resume", lambda attempt: invoked.append(attempt.id))
                self.assertEqual(invoked, [a.id])

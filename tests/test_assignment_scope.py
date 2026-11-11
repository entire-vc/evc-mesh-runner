"""Assignment scope is diagnosed before execution, without granting access. Only a card of another workspace
is refused: which projects a lane serves is Mesh's membership (the lane option ``project_ids`` was removed)."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mesh_runner.mesh import MeshError
from mesh_runner.selection import Policy, static_skip_reason
from mesh_runner.workspace_scope import METADATA_ONLY
from fakes import FakeMesh, calls, task
from test_runner import Bench


class AssignmentScope(Bench):
    def setup_scope(self):
        t = task(1)
        t.update(project_id="project-b", labels=["decision-request", METADATA_ONLY])
        mesh = FakeMesh([t])
        return t, mesh, self.runner(mesh)

    def foreign(self):
        """A card of another workspace than the lane's (FakeMesh.me is in ws1)."""
        t, mesh, r = self.setup_scope()
        t["workspace_id"] = "partner-workspace"
        return t, mesh, r

    def notices(self, mesh, t):
        return [c["body"] for c in mesh.comments_all(t["id"]) if "assignment_preflight=" in c["body"]]

    def test_a_card_of_any_project_mesh_admits_is_not_held(self):
        t, mesh, r = self.setup_scope()
        r.refresh_me()
        self.assertEqual(r._assignment_scope_reason(t, "work", publish=True), "")
        self.assertEqual(static_skip_reason(t, Policy(), dt.datetime.now(dt.timezone.utc)), "")
        self.assertFalse(hasattr(Policy(), "project_ids"))
        self.assertFalse(self.notices(mesh, t))

    def test_workspace_mismatch_has_no_automatic_grant_delta(self):
        t, mesh, r = self.foreign()
        r.refresh_me()
        reason = r._assignment_scope_reason(t, "work", publish=True)
        self.assertIn("workspace_scope_mismatch", reason)
        diagnostic = json.loads(self.notices(mesh, t)[0].split("assignment_preflight=", 1)[1])
        self.assertIsNone(diagnostic["required_delta"])
        self.assertIsNone(diagnostic["eligible_owner"])
        self.assertFalse(mesh.checkouts)

    def test_resume_scope_refusal_preserves_session_and_attempt(self):
        t, mesh, r = self.foreign()
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        sid = "11111111-1111-4111-8111-111111111111"
        r.store.update(a, state="ready", session_id=sid)
        for _ in range(3):
            r.resume_ready(1)
            r._resume_hold.clear()
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.session_id), ("ready", sid))
        self.assertEqual(len(self.notices(mesh, t)), 1)
        self.assertFalse(mesh.checkouts)
        self.assertFalse(calls(self.calls))

    def test_failed_publication_does_not_acknowledge_diagnosis(self):
        t, mesh, r = self.foreign()
        r.refresh_me()
        with mock.patch.object(mesh, "add_comment", side_effect=MeshError(503, "unavailable")):
            with self.assertRaises(MeshError):
                r.hold_reason(t, "work")
        r.hold_reason(t, "work")
        self.assertEqual(len(self.notices(mesh, t)), 1)


class RemovedProjectIds(unittest.TestCase):
    """A config written for 0.2.3 that still lists ``project_ids`` on a lane loads; the key is ignored with
    one log line naming the lane."""

    def test_a_config_that_still_carries_project_ids_loads(self):
        from mesh_runner import config as config_mod
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        p = Path(d.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n'
                     f'project_ids = ["p-tgpeek"]\n'
                     f'[[agents]]\nname = "b"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
        with self.assertLogs("mesh_runner", "WARNING") as logged:
            cfg = config_mod.load(p)
        self.assertEqual([a.name for a in cfg.agents], ["a", "b"])
        self.assertFalse(hasattr(cfg.agents[0].policy, "project_ids"))
        self.assertEqual(len(logged.output), 1)
        self.assertIn("agent a: project_ids is no longer read and is ignored", logged.output[0])

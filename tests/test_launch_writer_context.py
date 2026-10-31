"""Checked writer context at the real harness process boundary."""
import dataclasses
import json

from pathlib import Path

from fakes import FAKE_AGENT, FakeMesh, task, write_replies
from test_runner import Bench

KEYS = ("MESH_RUNNER_TASK", "MESH_RUNNER_LANE",
        "MESH_RUNNER_CHECKOUT_GENERATION", "MESH_RUNNER_CHECKOUT_REQUEST",
        "MESH_RUNNER_CHECKOUT_SESSION")


class LaunchWriterContext(Bench):
    def setUp(self):
        super().setUp()
        self.probe = self.root / "writer-context.json"
        prefix = ("import json, os\nfrom pathlib import Path\n"
                  "Path(os.environ['WRITER_PROBE']).write_text(json.dumps("
                  "{k:v for k,v in os.environ.items() if k.startswith('MESH_RUNNER_CHECKOUT_') "
                  "or k in ('MESH_RUNNER_TASK','MESH_RUNNER_LANE')}))\n")
        self.adapter.script.write_text(prefix + FAKE_AGENT)
        self.agent.env["WRITER_PROBE"] = str(self.probe)
        self.agent.env.update({key: "foreign-caller" for key in KEYS})
        self.agent.env["MESH_RUNNER_CHECKOUT_TOKEN"] = "must-not-reach-child"
        self.agent.inline_env = dict(self.agent.env)

    def launch(self, change=None, resume=False):
        t = task(1)
        mesh = FakeMesh([t])
        write_replies(self.replies, {"00000001": "↻ NEXT"})
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        self.assertTrue(r._acquire(a))
        if resume:
            r.store.update(a, state="ready", session_id="existing-native-session")
        if change:
            a = change(r, a)
        expected = ("ready",) if resume else ("starting",)
        self.assertTrue(r._spawn(a, "#00000001", resume=resume, expect=expected))
        proc = r.procs[a.id]
        try:
            self.assertEqual(proc.wait(timeout=10), 0,
                             Path(r.store.get(a.id).log_path).with_suffix(".stderr").read_text())
            context = json.loads(self.probe.read_text())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            r.procs.pop(a.id, None)
        return r, a, context

    def test_own_acquired_context_overrides_caller_and_excludes_token(self):
        r, a, context = self.launch()
        self.assertEqual(context, {
            "MESH_RUNNER_TASK": a.task_id, "MESH_RUNNER_LANE": self.agent.name,
            "MESH_RUNNER_CHECKOUT_GENERATION": str(a.checkout["generation"]),
            "MESH_RUNNER_CHECKOUT_REQUEST": a.checkout["request_id"],
            "MESH_RUNNER_CHECKOUT_SESSION": a.checkout["session_id"]})
        self.assertEqual(r.env["MESH_RUNNER_TASK"], "foreign-caller")
        self.assertEqual(r.env["MESH_RUNNER_LANE"], "foreign-caller")

    def test_resume_exports_checkout_session_not_native_harness_session(self):
        _, a, context = self.launch(resume=True)
        self.assertEqual(context["MESH_RUNNER_CHECKOUT_SESSION"], a.checkout["session_id"])
        self.assertNotEqual(context["MESH_RUNNER_CHECKOUT_SESSION"], a.session_id)

    def test_absent_checkout_cannot_inherit_caller_writer(self):
        def release(r, a):
            r._release_checkout(a)
            return a
        _, _, context = self.launch(release)
        self.assertEqual(context, {})

    def test_foreign_attempt_cannot_export_writer(self):
        _, _, context = self.launch(lambda r, a: dataclasses.replace(a, agent="other-lane"))
        self.assertEqual(context, {})

    def test_foreign_task_cannot_export_writer(self):
        _, _, context = self.launch(lambda r, a: dataclasses.replace(a, task_id=task(2)["id"]))
        self.assertEqual(context, {})

    def test_stale_checkout_cannot_export_writer(self):
        def replace_checkout(r, a):
            stale = dataclasses.replace(a)
            r.store.update(a, checkout={**a.checkout, "generation": a.checkout["generation"] + 1})
            return stale
        _, _, context = self.launch(replace_checkout)
        self.assertEqual(context, {})

    def test_invalid_generation_cannot_export_writer(self):
        def invalidate(r, a):
            r.store.update(a, checkout={**a.checkout, "generation": True})
            return a
        _, _, context = self.launch(invalidate)
        self.assertEqual(context, {})

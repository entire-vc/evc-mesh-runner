"""A card selects a permitted alt harness with `runner-harness:<h>`; the lane's global route is untouched."""
import sys
import unittest

from fakes import FakeMesh, FakeProfileAdapter, calls, task, write_replies, FUTURE_AT
from test_runner import Bench

HOME_ROUTE = [sys.executable, "-c", "print('X=1')"]


class TaskRoute(Bench):
    def lane(self, permitted):
        self.agent.harness = "claude"
        self.agent.env_command = HOME_ROUTE
        self.agent.task_route_harnesses = permitted
        r = self.runner(FakeMesh([]))
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "maker")}
        r._task = lambda tid: {"id": tid, "labels": self.labels}
        return r

    def test_labelled_card_routes_to_permitted_profile_and_back(self):
        r = self.lane(["codex"])
        self.labels = ["runner-harness:codex"]
        r._fresh_env("t1")
        self.assertEqual((r._routed_harness, r.adapter_for("codex").profile), ("codex", "maker"))
        self.labels = []
        r._fresh_env("t2")
        self.assertEqual(r._routed_harness, "claude")

    def test_unlisted_harness_or_unconfigured_lane_ignores_the_label(self):
        self.labels = ["runner-harness:codex"]
        r = self.lane([])
        r._fresh_env("t1")
        self.assertEqual(r._routed_harness, "claude")
        r = self.lane(["codex"])
        self.labels = ["runner-harness:evil"]
        r._fresh_env("t1")
        self.assertEqual(r._routed_harness, "claude")

    def test_quota_resume_preserves_the_cards_harness_and_profile(self):
        t = task(1, labels=["runner-harness:codex"])
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.task_route_harnesses = ["codex"]
        self.agent.env_command = HOME_ROUTE  # The global lane remains Claude.
        self.agent.alt = {"codex": {"profile": "maker"}}
        self.agent.inline_env.update(HOME=str(self.root))
        capacity = self.root / "account-capacity-ready"
        # External operator hook: real subprocesses supply fresh account admission.
        self.agent.start_gate_command = [sys.executable, "-c",
            "import os, pathlib, sys; "
            "required = os.environ.get('MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY') == '1'; "
            f"ready = pathlib.Path({str(capacity)!r}).exists(); "
            "print('ACCOUNT_CAPACITY_READY=1' if required and ready else 'account capacity unavailable'); "
            "sys.exit(75 if required and not ready else 0)"]
        write_replies(self.replies, {
            t["id"][:8]: "ERROR:usage limit reached",
            t["id"][:8] + ":resume":
                f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window",
        })
        r = self.runner(mesh)
        # External provider CLI: native Codex protocol, real child process and ACK.
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "maker", native="codex")}
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        self.assertEqual(a.harness, "codex")
        self.assertTrue(r._quota_admission_required(a))
        # The external CLI fixture inherits Claude's transcript lookup. Supply its
        # session in the isolated fixture HOME so native resume can find it.
        transcript = self.root / ".claude" / "projects" / "fixture" / (a.session_id + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        capacity.touch()
        r._resume_hold.clear()  # Advance only the fixture's local gate recheck.
        r.tick()
        self.settle_all(r)
        resumed = r.store.get(a.id)
        self.assertEqual(resumed.state, "waiting", r.store.events(a.id))
        self.assertEqual((r._routed_harness, resumed.harness), ("codex", "codex"))
        self.assertEqual(resumed.session_id, a.session_id)
        self.assertFalse(r._quota_admission_required(resumed))
        self.assertEqual(calls(self.calls)[-1]["profile"], "maker")
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])


if __name__ == "__main__":
    unittest.main()

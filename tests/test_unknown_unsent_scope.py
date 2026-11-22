"""A proven-unsent prompt whose scope changed while it waited (``prompt_retry_scope_changed``).

On 2026-11-16/17 twelve attempts went to ``unknown`` this way: three because the lane's workspaces came back in
another order, nine because routing moved the lane to another harness without quota proof. Their prompts were
proven never sent, so the quiet rule skipped them as "retryable", and nothing but an operator token ended them:
they stood 40 h with their cards, three of them questions other lanes waited on.

Now:
- the same leased workspaces in another order are the same scope: the exact prompt is sent as it would be;
- an unsent prompt held in ``unknown`` by a real scope change ends by the quiet rule like any other ``unknown``
  (process gone, evidence unchanged for UNKNOWN_QUIET_MINUTES): no replay, the card returns to the queue and starts
  in a fresh session, and runner.log says so.
"""
import hashlib
import json
import sys
import unittest
import uuid
from pathlib import Path

from fakes import calls
from mesh_runner import runner as runner_mod
from mesh_runner.workspace import RepoSpec
import test_headless_quota_unbound as quota
from test_headless_quota_unbound import LIMIT, SERVER, VERSION
from test_workspace import make_repo


def fixture_only(cls):
    """Reuse the base class's fixture without running its tests a second time."""
    for name in dir(cls):
        if name.startswith("test_") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@fixture_only
class UnsentScopeChanged(quota.HeadlessKimiQuota):

    def age_quiet_clock(self, minutes):
        key = "unknown_quiet:" + self.a.id
        started = self.r.store.mark(self.agent.name, self.t["id"], key)
        self.assertIsNotNone(started, "the quiet clock never started for the held unsent prompt")
        mark = json.loads(started[0])
        mark["since"] -= minutes * 60
        self.r.store.mark(self.agent.name, self.t["id"], key, json.dumps(mark))

    def comments(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])]

    def held_by_a_route_change(self):
        """The incident's state: a proven-unsent prompt, routing moved the lane without quota proof."""
        self.turn([VERSION], LIMIT)
        a = self.assert_kept_for_fresh_account()
        Path(self.intent["log_path"]).with_suffix(".stderr").write_text(SERVER)
        self.agent.start_gate_command = [sys.executable, "-c", "print('ACCOUNT_CAPACITY_READY=1')"]
        self.r._resume_one(a)
        a = self.r.store.get(a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_retry_scope_changed"), ["workspaces-or-harness"])
        self.assertTrue(a.wait["interactive_prompt_intent"]["retryable"])
        return a

    def test_an_unsent_prompt_held_by_a_scope_change_returns_the_card_to_the_queue(self):
        a = self.held_by_a_route_change()
        self.r.settle_finished()                          # quiet clock starts
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(a.id).state, "unknown")
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES - 1)
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(a.id).state, "unknown", "not before N minutes")
        self.age_quiet_clock(2)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.r.settle_finished()
        a = self.r.store.get(a.id)
        self.assertEqual(a.state, "completed")
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertEqual(len(self.details("quiet_resolution")), 1)
        self.assertIn("unsent prompt, scope changed", self.details("quiet_resolution")[0])
        archived = json.loads(self.r.store.mark(self.agent.name, self.t["id"], "operator_resolved_intent:" + a.id)[0])
        self.assertEqual(archived["intent"]["token"], self.intent["token"])
        self.assertTrue(any("unknown attempt ended after" in m and "the card returns to the queue" in m
                            and "#" + self.t["id"][:8] in m for m in logs.output), logs.output)
        self.assertTrue(any("last prompt was never sent" in c for c in self.comments()), self.comments())
        self.assertFalse(calls(self.calls), "the unsent prompt is never sent into the changed scope")
        # the card is free: no open attempt carries the old prompt
        self.assertEqual(self.r.store.by_state(self.agent.name, ("starting", "running", "ready", "waiting",
                                                                 "unknown")), [])

    def test_an_operator_token_still_ends_it_first(self):
        # control: the operator's token keeps its precedence over the quiet clock
        a = self.held_by_a_route_change()
        (self.cfg.state_dir / "resolve.d").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "resolve.d" / a.id).write_text("operator test\n")
        self.r.settle_finished()
        a = self.r.store.get(a.id)
        self.assertEqual(a.state, "completed")
        self.assertIn("operator_resolution", [k for _, k, _ in self.r.store.events(a.id)])
        self.assertEqual(self.details("quiet_resolution"), [])


@fixture_only
class WorkspaceOrder(quota.HeadlessKimiQuota):
    """Two repositories; the pending prompt names the same leased paths in the other order."""

    def setUp(self):
        super().setUp()
        lib = self.root / "lib"
        lib.mkdir()
        self.agent.repos.append(RepoSpec("lib", make_repo(lib), max_slots=4))
        self.r = self.runner(self.mesh)
        self.r.me = self.mesh.me()
        self.agent.start_gate_command = [sys.executable, "-c", "print('ACCOUNT_CAPACITY_READY=1')"]

    def ready_with_pinned(self, pinned):
        a = self.r.store.create(self.agent.name, self.t["id"], "claude", self.t["title"])
        leased = [str(self.r.pool.lease(a.id, "app")), str(self.r.pool.lease(a.id, "lib"))]
        log = self.root / f"{a.id}.turn2.claude.jsonl"
        log.write_text("")
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "turn": 2, "log_path": str(log),
                       "sha256": hashlib.sha256(self.PROMPT.encode()).hexdigest(), "prompt": self.PROMPT,
                       "kind": "headless", "harness": "claude", "session_id": "", "resume": False,
                       "workspaces": pinned(leased), "source_read_at": "pending-source", "retryable": True,
                       "prior": {"turns": 1, "session_id": "", "log_path": "", "turn_started_at": "",
                                 "source_read_at": "previous-source"}}
        self.assertTrue(self.r.store.update(a, state="ready", pid=0, turns=1, workspaces=leased,
                                            wait={"interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)
        return self.a

    def test_the_same_workspaces_in_another_order_send_the_exact_prompt(self):
        a = self.ready_with_pinned(lambda leased: list(reversed(leased)))
        self.r._resume_one(a)
        current = self.r.store.get(a.id)
        self.assertEqual(self.details("prompt_retry_scope_changed"), [])
        self.assertEqual(current.state, "running")
        self.assertEqual(self.r.procs[a.id].wait(timeout=10), 0)
        seen = calls(self.calls)
        self.assertEqual(len(seen), 1)
        self.assertIn(self.PROMPT, seen[0]["prompt"])

    def test_other_workspaces_still_hold_the_prompt(self):
        # control: a changed set of paths is a changed scope
        a = self.ready_with_pinned(lambda leased: leased[:1])
        self.r._resume_one(a)
        self.assertEqual(self.r.store.get(a.id).state, "unknown")
        self.assertEqual(self.details("prompt_retry_scope_changed"), ["workspaces-or-harness"])
        self.assertFalse(calls(self.calls))


class SameWorkspaces(unittest.TestCase):

    def test_same_workspaces_is_a_set_of_paths(self):
        self.assertTrue(runner_mod.same_workspaces(["/a", "/b"], ["/b", "/a"]))
        self.assertFalse(runner_mod.same_workspaces(["/a"], ["/a", "/b"]))
        self.assertFalse(runner_mod.same_workspaces(["/a", "/a"], ["/a", "/b"]))
        self.assertFalse(runner_mod.same_workspaces(None, []))
        self.assertTrue(runner_mod.same_workspaces([], []))

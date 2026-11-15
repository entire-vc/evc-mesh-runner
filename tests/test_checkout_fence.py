"""The checkout fence: the lane's result writes carry the running attempt's lock, a lost lock stops the turn.

Scenario tests through the real loop (fake Mesh, real child processes). The agent's Mesh MCP is the
other half of the contract (evc-mesh-mcp): here the test plays it, reading the fence file the Runner
publishes and appending to the refusal journal the way the MCP does.
"""
import datetime as dt
import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

from fakes import AGENT_ID, FakeMesh, HarnessGate, calls, finish, task, wait_card, write_replies
from test_runner import Bench

from mesh_runner import checkout_fence
from mesh_runner.mesh import Checkout, MeshClient, MeshError


class CountingMesh(FakeMesh):
    """Every checkout is a new lock: a new token and the next generation, as Mesh issues them."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.generation = 0

    def checkout(self, tid, session_id, request_id, ttl_minutes=120):
        if tid in self.refuse_checkout or tid in self.checkouts:
            raise MeshError(409, "checked out")
        self.generation += 1
        self.checkouts[tid] = {"session": session_id}
        return Checkout(tid, f"tok-{self.generation}", self.generation, "", session_id, request_id)


class RefusingMesh(FakeMesh):
    """Mesh refusing the Runner's fenced writes once the lock is gone (``refuse``), the way MeshClient._fenced
    reports such a refusal: the callback first, then the 409."""
    refuse = False

    def _fenced(self, tid):
        fence = self.fence_for(tid) if self.refuse and getattr(self, "fence_for", None) else {}
        if fence:
            self.refused_writes = getattr(self, "refused_writes", 0) + 1
            body = {"error_code": checkout_fence.LOCK_LOST, "context": {"reassigned": False}}
            self.on_fence_refused(tid, checkout_fence.LOCK_LOST, body)
            raise MeshError(409, "checkout_precondition_failed", body)

    def add_comment(self, tid, body, author=AGENT_ID, at=None, key=""):
        if author == AGENT_ID:
            self._fenced(tid)
        return super().add_comment(tid, body, author=author, at=at, key=key)

    def move(self, tid, status_id, expected_version=None, expected_status_id=None):
        self._fenced(tid)
        return super().move(tid, status_id, expected_version, expected_status_id)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class FenceScenarios(Bench):
    def start_gated(self, mesh, t, reply=None):
        gate = HarnessGate(self.root / f"gate-{t['id'][:8]}")
        write_replies(self.replies, {t["id"][:8]: gate.reply(reply if reply is not None else finish())})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state(self.agent.name, ("starting", "running"))[0]
        deadline = time.monotonic() + 10
        while not gate.started.exists():
            self.assertLess(time.monotonic(), deadline, "fake harness did not reach its gate")
            time.sleep(0.01)
        r.tick()                                     # the delivery is acknowledged: the turn runs
        return r, r.store.get(a.id), gate

    def mcp_refusal(self, r, a, code, **extra):
        """What the MCP appends when Mesh refused one of its fenced writes."""
        path = r._fence().refusals_path(a.task_id)
        row = {"attempt_id": a.id, "turn": r.store.get(a.id).turns, "code": code, "at": now_iso(),
               "tool": "add_comment", **extra}
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    # ---------------------------------------------------------------- the fence the MCP reads
    def test_turn_publishes_its_lock_and_input_revision_and_withdraws_it_at_the_end(self):
        t = task(1, input_revision=7)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        data = r._fence().read(t["id"])
        self.assertEqual(data["attempt_id"], a.id)
        self.assertEqual(data["checkout_token"], a.checkout["token"])
        self.assertEqual(data["generation"], a.checkout["generation"])
        self.assertEqual(data["input_revision"], 7)
        self.assertEqual(checkout_fence.headers({"token": data["checkout_token"]}, data["input_revision"]),
                         {"X-Mesh-Checkout-Token": a.checkout["token"], "X-Mesh-Expected-Input-Revision": "7"})
        path = r._fence().path(t["id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        gate.release()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "completed")
        self.assertIsNone(r._fence().read(t["id"]))      # the lock is gone with the attempt

    def test_runner_own_writes_on_a_held_card_carry_the_lock(self):
        t = task(2)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        self.assertEqual(r._fence_headers(t["id"]), {"X-Mesh-Checkout-Token": a.checkout["token"]})
        self.assertEqual(r._fence_headers(task(3)["id"]), {})          # a card the lane does not hold
        gate.release()
        self.settle_all(r)
        self.assertEqual(r._fence_headers(t["id"]), {})               # released: unfenced, as before

    def test_resumed_session_writes_with_the_new_attempts_lock(self):
        t, other = task(4), task(5, assignee_id="agent-lead")
        mesh = CountingMesh([t, other], max_concurrent=1)
        gate = HarnessGate(self.root / "resume")
        write_replies(self.replies, {t["id"][:8]: wait_card(other["id"]),
                                     t["id"][:8] + ":resume": gate.reply(finish())})
        r = self.runner(mesh)
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        # wait_for released the lock in Mesh: no fence is left for the waiting session
        self.assertIsNone(r._fence().read(t["id"]))
        mesh.set_category(other["id"], "done")
        r.tick()
        deadline = time.monotonic() + 15
        while not gate.started.exists():
            r.tick()
            self.assertLess(time.monotonic(), deadline, "the resumed turn did not start")
            time.sleep(0.05)
        a = r.store.by_state(self.agent.name, ("starting", "running"))[0]
        data = r._fence().read(t["id"])
        self.assertEqual((data["checkout_token"], data["generation"]), ("tok-2", 2))
        self.assertEqual(data["attempt_id"], a.id)
        gate.release()
        self.settle_all(r)

    # ---------------------------------------------------------------- a lost lock stops the turn
    def assert_stopped(self, r, t, a, reason):
        self.settle_all(r, timeout=15)
        ended = r.store.get(a.id)
        self.assertEqual(ended.state, "failed")
        self.assertIn("checkout lost: " + reason, ended.note)
        self.assertIsNone(r._fence().read(t["id"]))
        self.assertFalse(ended.checkout)
        self.assertNotEqual(ended.state, "unknown")

    def test_fence_not_written_holds_the_start_and_no_turn_runs_unfenced(self):
        t = task(20)
        mesh = FakeMesh([t], max_concurrent=1)
        blocker = self.root / "not-a-dir"
        blocker.write_text("")
        write_replies(self.replies, {t["id"][:8]: finish()})
        with mock.patch.dict(os.environ, {checkout_fence.ENV_DIR: str(blocker / "fence")}):
            r = self.runner(mesh)
            r.tick()
            time.sleep(0.5)
            r.tick()
        self.assertEqual(calls(self.calls), [])                       # no harness was started
        rows = r.store.by_state(self.agent.name, ("starting", "running", "unknown", "ready", "waiting",
                                                   "completed", "failed", "aborted"))
        self.assertEqual(len(rows), 1)
        a = rows[0]
        self.assertNotIn(a.state, ("running", "unknown"))
        self.assertFalse(a.pid)
        self.assertIn("fence_publish_failed", [k for _, k, _ in r.store.events(a.id)])
        self.assertNotIn(t["id"], mesh.checkouts)                       # the lock was handed back

    def test_runner_write_refused_while_settling_ends_the_attempt_and_stops_settlement(self):
        t = task(21)
        mesh = RefusingMesh([t], max_concurrent=1)
        self.cfg.max_no_outcome = 0
        r, a, gate = self.start_gated(mesh, t, reply="looked at it")    # a turn without an outcome
        mesh.refuse = True                    # the lock is gone: the Runner's next fenced write is refused
        gate.release()
        self.settle_all(r, timeout=15)
        ended = r.store.get(a.id)
        self.assertEqual(ended.state, "failed")
        self.assertIn("checkout lost: ", ended.note)
        self.assertEqual(mesh.refused_writes, 1)                       # settlement stopped at the first refusal
        self.assertFalse(ended.checkout)
        self.assertIsNone(r._fence().read(t["id"]))
        self.assertNotIn("error:settle", [k for _, k, _ in r.store.events(a.id)])

    def test_takeover_refused_heartbeat_stops_the_turn_and_writes_nothing(self):
        t = task(6)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        mesh.checkouts[t["id"]] = {"session": "lane-b"}                 # another holder took the lock
        mesh.tasks[t["id"]].update(checked_out_by="agent-lead", checkout_session_id="lane-b")
        mesh.heartbeat = lambda co, ttl_minutes=120: (_ for _ in ()).throw(MeshError(409, "lost"))
        before = len(mesh.comment_log.get(t["id"], []))
        r.tick()
        self.assert_stopped(r, t, a, "taken over")
        self.assertEqual(len(mesh.comment_log.get(t["id"], [])), before)   # nothing written after the stop
        self.assertIn("checkout_lost", [k for _, k, _ in r.store.events(a.id)])

    def test_self_checkout_by_the_agent_is_named_and_stops_the_turn(self):
        t = task(7)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        # the agent called checkout_task itself: same identity, a new lock under another session
        mesh.checkouts.pop(t["id"])
        mesh.tasks[t["id"]].update(checked_out_by=AGENT_ID, checkout_session_id="agent-own-session")
        r.tick()
        self.assert_stopped(r, t, a, "self-checkout")

    def test_refused_fenced_write_of_the_agent_stops_the_turn_before_the_heartbeat(self):
        t = task(8)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        self.cfg.heartbeat_seconds = 3600                               # no heartbeat in this test
        mesh.tasks[t["id"]]["assignee_id"] = "agent-lead"                # reassigned; Mesh keeps the lock
        self.mcp_refusal(r, a, checkout_fence.LOCK_LOST, reassigned=True)
        beats = mesh.heartbeats
        r.tick()
        self.assertEqual(mesh.heartbeats, beats)
        self.assert_stopped(r, t, a, "reassigned")

    def test_closed_card_keeps_its_existing_path(self):
        t = task(9)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        mesh.set_category(t["id"], "done")
        mesh.checkouts.pop(t["id"])
        r.tick()                                                         # first closed observation: left alone
        self.assertEqual(r.store.get(a.id).state, "running")
        r.tick()
        self.settle_all(r)
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertNotIn("lock_lost_stop", [k for _, k, _ in r.store.events(a.id)])

    # ---------------------------------------------------------------- input_revision
    def test_finish_refused_for_a_changed_task_is_not_a_turn_without_an_outcome(self):
        t = task(10, input_revision=3)
        mesh = FakeMesh([t], max_concurrent=1)
        gate = HarnessGate(self.root / "revision")
        write_replies(self.replies, {t["id"][:8]: gate.reply("I tried to close it"),
                                     t["id"][:8] + ":resume": finish()})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state(self.agent.name, ("starting", "running"))[0]
        deadline = time.monotonic() + 10
        while not gate.started.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        mesh.tasks[t["id"]]["input_revision"] = 4                       # the description was edited
        self.mcp_refusal(r, r.store.get(a.id), checkout_fence.REVISION_CHANGED, tool="finish_task",
                         input_revision=4)
        gate.release()
        self.settle_all(r)
        kinds = [k for _, k, _ in r.store.events(a.id)]
        self.assertIn("input_revision_changed", kinds)
        self.assertNotIn("no_outcome", kinds)                             # not a turn without an outcome
        self.assertEqual(r.store.get(a.id).state, "completed")
        prompts = [json.loads(l)["prompt"] for l in self.calls.read_text().splitlines()]
        self.assertIn("input_revision_changed", prompts[1])              # the next turn is told to re-read
        self.assertIn("get_task", prompts[1])
        published = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "fence_published"]
        self.assertEqual([p["input_revision"] for p in published], [3, 4])   # re-read at every turn start

    # ---------------------------------------------------------------- rollback
    def test_rollback_key_restores_todays_behaviour(self):
        self.cfg.checkout_fence = False
        t = task(11, input_revision=1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, a, gate = self.start_gated(mesh, t)
        self.assertIsNone(r._fence().read(t["id"]))
        self.assertFalse(any(checkout_fence.DEFAULT_DIR.rglob(f"{t['id']}.json")))
        self.assertEqual(r._fence_headers(t["id"]), {})
        mesh.checkouts.pop(t["id"])                                      # lock lost, card open
        r.tick()
        cur = r.store.get(a.id)
        self.assertEqual(cur.state, "running")                          # today: the turn runs on
        self.assertFalse(cur.checkout)
        self.assertNotIn("lock_lost_stop", [k for _, k, _ in r.store.events(a.id)])
        gate.release()
        self.settle_all(r)


class FencedClient(unittest.TestCase):
    """MeshClient: the fence goes on comment and move only when the lane holds the card."""

    def client(self, status=200, body=b"{}"):
        sent = []

        def opener(req, timeout=None):
            sent.append(req)
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
            return io.BytesIO(body)

        c = MeshClient("https://mesh.test", "k", opener=opener)
        return c, sent

    def test_headers_sent_only_with_a_held_lock(self):
        c, sent = self.client()
        c.fence_for = lambda tid: {"X-Mesh-Checkout-Token": "t1"} if tid == "held" else {}
        c.add_comment("held", "x")
        c.add_comment("other", "x")
        c.move("held", "s1")
        self.assertEqual(sent[0].get_header("X-mesh-checkout-token"), "t1")
        self.assertIsNone(sent[1].get_header("X-mesh-checkout-token"))
        self.assertEqual(sent[2].get_header("X-mesh-checkout-token"), "t1")

    def test_keyed_comment_carries_both_the_fence_and_the_key(self):
        c, sent = self.client()
        c.fence_for = lambda tid: {"X-Mesh-Checkout-Token": "t1"}
        c.add_comment("held", "x", key="banner:a1:3")
        self.assertEqual(sent[0].get_header("X-mesh-checkout-token"), "t1")
        self.assertTrue(sent[0].get_header("Idempotency-key"))

    def test_refused_fenced_write_reports_the_code(self):
        body = json.dumps({"error_code": "checkout_precondition_failed", "context": {"reassigned": True}}).encode()
        c, sent = self.client(409, body)
        heard = []
        c.fence_for = lambda tid: {"X-Mesh-Checkout-Token": "t1"}
        c.on_fence_refused = lambda tid, code, b: heard.append((tid, code, b["context"]["reassigned"]))
        with self.assertRaises(MeshError):
            c.add_comment("held", "x")
        self.assertEqual(heard, [("held", "checkout_precondition_failed", True)])

    def test_unfenced_409_is_not_a_lost_lock(self):
        c, sent = self.client(409, json.dumps({"error_code": "cas_conflict"}).encode())
        heard = []
        c.fence_for = lambda tid: {}
        c.on_fence_refused = lambda *a: heard.append(a)
        with self.assertRaises(MeshError):
            c.move("held", "s1", expected_version=1)
        self.assertEqual(heard, [])


class FenceFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fence = checkout_fence.FenceDir(Path(self.tmp.name), "agent-x")
        self.tid = task(1)["id"]

    def test_withdraw_leaves_a_later_attempts_file(self):
        self.fence.publish(self.tid, "attempt-2", {"token": "t2", "generation": 2, "session_id": "s"}, 1)
        self.fence.withdraw(self.tid, "attempt-1")
        self.assertEqual(self.fence.read(self.tid)["attempt_id"], "attempt-2")
        self.fence.withdraw(self.tid, "attempt-2")
        self.assertIsNone(self.fence.read(self.tid))

    def test_sweep_drops_files_of_ended_attempts(self):
        other = task(2)["id"]
        self.fence.publish(self.tid, "live", {"token": "t"}, None)
        self.fence.publish(other, "ended", {"token": "t"}, None)
        self.assertEqual(self.fence.sweep({self.tid: "live"}), [other])
        self.assertIsNotNone(self.fence.read(self.tid))

    def test_refusals_of_other_attempts_and_older_turns_are_ignored(self):
        p = self.fence.refusals_path(self.tid)
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"attempt_id": "old", "code": "checkout_precondition_failed", "at": "2026-11-12T10:00:00Z"},
                {"attempt_id": "a", "code": "checkout_precondition_failed", "at": "2026-11-12T09:00:00Z"},
                {"attempt_id": "a", "code": "input_revision_changed", "at": "2026-11-12T11:00:00Z",
                 "input_revision": 5},
                "not json"]
        p.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows))
        got = self.fence.refusals(self.tid, "a", since="2026-11-12T10:00:00.000+00:00")
        self.assertEqual([(r.code, r.input_revision) for r in got], [("input_revision_changed", 5)])

    def test_disabled_dir_does_nothing(self):
        off = checkout_fence.FenceDir(None, "agent-x")
        self.assertFalse(off.publish(self.tid, "a", {"token": "t"}, 1))
        self.assertIsNone(off.read(self.tid))
        self.assertEqual(off.refusals(self.tid, "a"), [])


if __name__ == "__main__":
    unittest.main()

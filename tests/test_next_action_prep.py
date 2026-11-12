import copy
import json
import unittest
from types import SimpleNamespace as NS

from mesh_runner import next_action
from mesh_runner.mesh import MeshError
from mesh_runner.runner import AgentRunner
from fakes import FakeMesh, calls, task, write_replies, FUTURE_AT, wait_time, wait_card
from test_runner import Bench

TID = "0000aaaa-0000-4000-8000-000000000001"
EDGE = "0000bbbb-0000-4000-8000-000000000002"
BLK = "0000cccc-0000-4000-8000-000000000003"

PROJ = {"version": 1, "task_id": TID, "generation": "v1:aa", "next_action": "continue_prep",
        "stages": [{"stage": "prep", "ready": True}, {"stage": "final_acceptance", "ready": False}],
        "requires": [{"edge_id": EDGE, "from_task": BLK, "condition": "deployed",
                      "gates": ["final_acceptance"], "released": False}]}
EDGES = [{"id": EDGE, "condition": "deployed", "depends_on_task_id": BLK, "dependency_type": "blocks",
          "condition_satisfied": False}]


class Store:
    def __init__(self):
        self.marks, self.events = {}, []

    def mark(self, agent, task, key, value=None):
        if value is not None:
            self.marks[(agent, task, key)] = (value, "t")
            return None
        return self.marks.get((agent, task, key))

    def event(self, aid, kind, text):
        self.events.append((aid, kind, text))


class Clearance(unittest.TestCase):
    def proj(self, **kw):
        p = copy.deepcopy(PROJ)
        p.update(kw)
        return p

    def test_ready_prep_with_final_only_gate_clears(self):
        self.assertEqual(next_action.clearance(TID, EDGES, PROJ), ("v1:aa", ""))

    def test_gate_on_earlier_stage_stays_held(self):
        p = self.proj(stages=[{"stage": "design", "ready": True}, {"stage": "prep", "ready": True},
                              {"stage": "final_acceptance", "ready": False}],
                      requires=[dict(PROJ["requires"][0], gates=["design"])])
        self.assertEqual(next_action.clearance(TID, EDGES, p)[0], "")

    def test_ambiguous_projection_stays_held(self):
        prep = {"stage": "prep", "ready": True}
        final = {"stage": "final_acceptance", "ready": False}
        req = PROJ["requires"][0]
        for p in (
            # duplicate prep: first not ready, last ready, gate on prep
            self.proj(stages=[{"stage": "prep", "ready": False}, prep, final],
                      requires=[dict(req, gates=["prep"])]),
            self.proj(stages=[prep, prep, final]),
            # two requirements with one edge_id: first gates prep, last only a later stage
            self.proj(requires=[dict(req, gates=["prep"]), dict(req, gates=["final_acceptance"])]),
            self.proj(requires=[dict(req, gates=["final_acceptance"]), dict(req, gates=["prep"])]),
        ):
            self.assertEqual(next_action.clearance(TID, EDGES, p)[0], "", p)
        self.assertEqual(next_action.clearance(TID, EDGES + EDGES, PROJ)[0], "")
        # gate prep never counts as later, whatever the stage order
        p = self.proj(stages=[final, prep], requires=[dict(req, gates=["prep"])])
        self.assertEqual(next_action.clearance(TID, EDGES, p)[0], "")

    def test_malformed_projection_never_raises(self):
        junk = [{}, [], None, 1, "x", [[]], [{}]]
        for j in junk:
            for p in (self.proj(stages=j), self.proj(requires=j), self.proj(stages=[j]),
                      self.proj(stages=[{"stage": j, "ready": True}]),
                      self.proj(requires=[dict(PROJ["requires"][0], gates=j)]),
                      self.proj(requires=[dict(PROJ["requires"][0], gates=[j])]),
                      self.proj(requires=[dict(PROJ["requires"][0], edge_id=j)]), j):
                self.assertEqual(next_action.clearance(TID, EDGES, p)[0], "", p)
        self.assertEqual(next_action.clearance(TID, [dict(EDGES[0], id=[])], PROJ)[0], "")

    def test_red_cases_stay_held(self):
        bad = [
            self.proj(version=2), self.proj(version=True), self.proj(version=1.0), self.proj(task_id="x"), self.proj(generation=""),
            self.proj(next_action="wait_requirement"), self.proj(next_action="final_acceptance"),
            self.proj(stages=[{"stage": "prep", "ready": False}]),
            self.proj(requires=[dict(PROJ["requires"][0], gates=["prep", "final_acceptance"])]),
            self.proj(requires=[dict(PROJ["requires"][0], gates=["unknown_stage"])]),
            self.proj(requires=[dict(PROJ["requires"][0], gates=[])]),
            self.proj(requires=[dict(PROJ["requires"][0], gates=["final_acceptance", "unknown_stage"])]),
            self.proj(requires=[dict(PROJ["requires"][0], released=True)]),
            self.proj(requires=[]),
        ]
        for p in bad:
            self.assertEqual(next_action.clearance(TID, EDGES, p)[0], "", p)
        self.assertEqual(next_action.clearance(TID, [dict(EDGES[0], condition="task_done")], PROJ)[0], "")
        self.assertEqual(next_action.clearance(TID, [], PROJ)[0], "")
        self.assertEqual(next_action.clearance(TID, EDGES, None)[0], "")


def runner(flag=True, projection=PROJ, err=None):
    r = AgentRunner.__new__(AgentRunner)
    r.agent = NS(name="A", next_action_prep=flag)
    r.store = Store()
    r._prep_pending = {}

    def na(tid):
        if err:
            raise MeshError(err, "x")
        return projection
    r.mesh = NS(next_action=na)
    return r


class RunnerHook(unittest.TestCase):
    task = {"id": TID}

    def test_off_by_default(self):
        r = runner(flag=False)
        self.assertFalse(r._prep_cleared(self.task, EDGES))
        self.assertEqual(r._prep_pending, {})

    def test_cleared_once_per_generation_after_real_dispatch(self):
        r = runner()
        self.assertTrue(r._prep_cleared(self.task, EDGES))
        self.assertEqual(r._prep_pending, {TID: "v1:aa"})
        # the start was refused: nothing recorded, the same generation is offered again
        self.assertTrue(r._prep_cleared(self.task, EDGES))
        next_action.record(r.store, "A", TID, "v1:aa", "att1")           # dispatched
        self.assertFalse(r._prep_cleared(self.task, EDGES))              # re-read: no duplicate
        self.assertEqual(r._prep_pending, {})
        r.mesh = NS(next_action=lambda t: dict(PROJ, generation="v1:bb"))
        self.assertTrue(r._prep_cleared(self.task, EDGES))                # new generation: one more

    def test_unreadable_projection_holds(self):
        self.assertFalse(runner(err=500)._prep_cleared(self.task, EDGES))


class SelectorIntegration(Bench):
    """The real loop: _hold_reason clears the typed blocker, _start_one records the generation."""

    def setUp(self):
        super().setUp()
        self.t, self.blocker = task(1), task(9, cat="review")
        self.edge = {"id": "e1", "depends_on_task_id": self.blocker["id"], "dependency_type": "blocks",
                     "condition": "deployed", "condition_satisfied": False}
        self.proj = {"version": 1, "task_id": self.t["id"], "generation": "v1:aa", "next_action": "continue_prep",
                     "stages": [{"stage": "prep", "ready": True}, {"stage": "final_acceptance", "ready": False}],
                     "requires": [{"edge_id": "e1", "gates": ["final_acceptance"], "released": False}]}
        self.reads = []
        self.mesh = FakeMesh([self.t], max_concurrent=1)
        self.mesh.tasks[self.blocker["id"]] = self.blocker
        self.mesh.deps[self.t["id"]] = [self.edge]
        self.mesh.next_action = lambda tid: self.reads.append(tid) or self.proj
        write_replies(self.replies, {self.t["id"][:8]: wait_time(why="prep done")})

    def starts(self):
        return [c for c in calls(self.calls) if c["task"] == "00000001"]

    def mark(self, r):
        m = r.store.mark(self.agent.name, self.t["id"], next_action.MARK)
        return json.loads(m[0]) if m else None

    def test_off_keeps_the_task_held_without_reading_the_projection(self):
        r = self.runner(self.mesh)
        self.assertTrue(r.hold_reason(self.t, "work").startswith("blocked by"))
        r.tick()
        self.assertEqual((self.starts(), self.reads), ([], []))

    def test_on_starts_prep_once_and_records_the_real_attempt(self):
        self.agent.next_action_prep = True
        r = self.runner(self.mesh)
        self.assertEqual(r.hold_reason(self.t, "work"), "")
        self.settle_all(r)
        self.assertEqual(len(self.starts()), 1)
        rec = self.mark(r)
        self.assertEqual(rec["generation"], "v1:aa")
        self.assertEqual(r.store.get(rec["attempt"]).task_id, self.t["id"])
        r._hold_cache.clear()
        self.assertFalse(r._prep_cleared(self.t, [self.edge]))                      # same generation: no duplicate
        self.assertEqual(self.mesh.deps[self.t["id"]][0]["condition_satisfied"], False)   # edge stays open
        self.proj = dict(self.proj, generation="v1:bb")
        self.assertTrue(r._prep_cleared(self.t, [self.edge]))                       # new generation: one more

    def test_a_refused_start_records_nothing_and_is_offered_again(self):
        self.agent.next_action_prep = True
        self.mesh.refuse_checkout.add(self.t["id"])
        r = self.runner(self.mesh)
        r.tick()
        self.assertEqual((self.starts(), self.mark(r)), ([], None))
        self.mesh.refuse_checkout.clear()
        r._task_backoff.clear()
        r._hold_cache.clear()
        self.settle_all(r)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.mark(r)["generation"], "v1:aa")

    def test_new_generation_wakes_the_waiting_attempt_once(self):
        """After the first prep the attempt stands in a card WAIT and the selector skips it; a new
        generation must still reach the consumer: one wake, same attempt, edge stays open."""
        self.agent.next_action_prep = True
        self.edge["task_id"] = self.t["id"]            # a well-formed Mesh row, as the stale-observation guard requires
        write_replies(self.replies, {self.t["id"][:8]: wait_card(self.blocker["id"])})
        r = self.runner(self.mesh)
        self.settle_all(r)
        first = self.mark(r)
        self.assertEqual(first["generation"], "v1:aa")
        att = r.store.get(first["attempt"])
        self.assertEqual(att.state, "waiting")
        r.tick()
        self.assertEqual(r.store.get(first["attempt"]).state, "waiting")     # same generation: no wake
        self.proj = dict(self.proj, generation="v1:bb")
        r._last_wake_check.clear()
        r._hold_cache.clear()
        r.tick()
        again = self.mark(r)
        self.assertEqual((again["generation"], again["attempt"]), ("v1:bb", first["attempt"]))
        self.assertIn(r.store.get(first["attempt"]).state, ("ready", "running", "starting"))
        self.assertEqual(r.store.events_on_task(self.t["id"], "next_action_prep"), 2)   # first prep + this wake
        self.assertEqual(self.mesh.deps[self.t["id"]][0]["condition_satisfied"], False)
        self.assertFalse(r._wake_prep_generation(r.store.get(first["attempt"]), self.t, []))   # same generation again


if __name__ == "__main__":
    unittest.main()

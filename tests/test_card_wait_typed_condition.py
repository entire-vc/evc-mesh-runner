"""``wait_for on=card target_condition=<typed condition>``: the edge this wait creates carries that condition
instead of the broad task_done. The plain form keeps creating task_done (unchanged)."""
import datetime as dt
import unittest

from fakes import FakeMesh, task, typed, wait_card
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench

NOW = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc)


def declared(condition=""):
    return typed(wait_card("deadbeef", condition=condition), now=NOW)


class Grammar(unittest.TestCase):
    def test_typed_card_wait_carries_its_condition(self):
        for cond in ("access_ready", "contract_ready", "artifact_published", "shipped", "deployed", "accepted"):
            o = declared(cond)
            self.assertEqual((o.kind, o.target), ("card", "deadbeef"), cond)
            self.assertEqual(o.extra.get("card_condition"), cond)

    def test_plain_card_wait_is_unchanged(self):
        o = declared()
        self.assertEqual((o.kind, o.target, o.extra.get("card_condition", "")), ("card", "deadbeef", ""))

    def test_unknown_condition_is_not_a_wait(self):
        for bad in ("done", "deployed,accepted", "DEPLOYED x"):
            self.assertEqual(declared(bad).kind, "unparsed", bad)


    def test_task_done_condition_is_the_plain_wait(self):
        o = declared("task_done")
        self.assertEqual((o.kind, o.extra.get("card_condition", "")), ("card", ""))


class Edge(Bench):
    def flow(self):
        t, target = task(1), task(3)
        mesh = FakeMesh([t, target])
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        r = AgentRunner(self.cfg, self.agent, Store(self.root / "typed.sqlite"), mesh=mesh, adapter=self.adapter)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        return r, a, t, target

    def wait(self, target, condition=""):
        return {"kind": "card", "target": target["id"][:8], "card_condition": condition, "since": "2026-11-09T00:00:00Z",
                "started": "2026-11-09T00:00:00Z", "fx": True}

    def test_typed_wait_creates_the_typed_edge_not_task_done(self):
        r, a, t, g = self.flow()
        w = self.wait(g, "deployed")
        r.effects._add_edge(a, w)
        rows = r.mesh.dependencies(t["id"])["outgoing"]
        self.assertEqual([e["condition"] for e in rows], ["deployed"])
        self.assertEqual((w["edge"], w["edge_condition"]), (rows[0]["id"], "deployed"))

    def test_plain_wait_still_creates_task_done(self):
        r, a, t, g = self.flow()
        w = self.wait(g, )
        r.effects._add_edge(a, w)
        self.assertEqual([e["condition"] for e in r.mesh.dependencies(t["id"])["outgoing"]], ["task_done"])
        self.assertNotIn("edge_condition", w)

    def test_cleanup_removes_its_own_typed_edge_but_not_a_changed_one(self):
        r, a, t, g = self.flow()
        w = self.wait(g, "deployed")
        r.effects._add_edge(a, w)
        self.assertTrue(r.effects._edge_still_ours(a, w))                 # still the shape it was created with
        r.mesh.edges[t["id"]][0]["condition"] = "accepted"               # an owner retyped it
        self.assertFalse(r.effects._edge_still_ours(a, w))


if __name__ == "__main__":
    unittest.main()

"""``⏳ WAIT card:#X until=<typed condition>``: the edge this WAIT creates carries that condition
instead of the broad task_done. The plain form keeps creating task_done (unchanged)."""
import datetime as dt
import unittest

from fakes import FakeMesh, task
from mesh_runner import outcome
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench

NOW = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc)


def declared(line):
    return outcome.parse_line(line, NOW)


class Grammar(unittest.TestCase):
    def test_typed_card_wait_parses_with_its_condition(self):
        for cond in ("access_ready", "contract_ready", "artifact_published", "shipped", "deployed", "accepted"):
            o = declared(f"⏳ WAIT card:#deadbeef until={cond}")
            self.assertEqual((o.kind, o.target), ("card", "deadbeef"), cond)
            self.assertEqual(outcome.card_condition(o.line), cond)

    def test_plain_card_wait_is_unchanged(self):
        o = declared("⏳ WAIT card:#deadbeef")
        self.assertEqual((o.kind, o.target, outcome.card_condition(o.line)), ("card", "deadbeef", ""))
        o = declared("⏳ WAIT card:#deadbeef: explanation")
        self.assertEqual((o.kind, outcome.card_condition(o.line)), ("card", ""))

    def test_unknown_condition_is_not_a_wait(self):
        for bad in ("until=done", "until=task_done", "until=", "until=deployed,accepted", "until=DEPLOYED x"):
            self.assertEqual(declared(f"⏳ WAIT card:#deadbeef {bad}").kind, "unparsed", bad)


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

    def wait(self, target, line):
        return {"kind": "card", "target": target["id"][:8], "line": line, "since": "2026-11-09T00:00:00Z",
                "started": "2026-11-09T00:00:00Z", "fx": True}

    def test_typed_wait_creates_the_typed_edge_not_task_done(self):
        r, a, t, g = self.flow()
        w = self.wait(g, f"⏳ WAIT card:#{g['id'][:8]} until=deployed")
        r.effects._add_edge(a, w)
        rows = r.mesh.dependencies(t["id"])["outgoing"]
        self.assertEqual([e["condition"] for e in rows], ["deployed"])
        self.assertEqual((w["edge"], w["edge_condition"]), (rows[0]["id"], "deployed"))

    def test_plain_wait_still_creates_task_done(self):
        r, a, t, g = self.flow()
        w = self.wait(g, f"⏳ WAIT card:#{g['id'][:8]}")
        r.effects._add_edge(a, w)
        self.assertEqual([e["condition"] for e in r.mesh.dependencies(t["id"])["outgoing"]], ["task_done"])
        self.assertNotIn("edge_condition", w)

    def test_cleanup_removes_its_own_typed_edge_but_not_a_changed_one(self):
        r, a, t, g = self.flow()
        w = self.wait(g, f"⏳ WAIT card:#{g['id'][:8]} until=deployed")
        r.effects._add_edge(a, w)
        self.assertTrue(r.effects._edge_still_ours(a, w))                 # still the shape it was created with
        r.mesh.edges[t["id"]][0]["condition"] = "accepted"               # an owner retyped it
        self.assertFalse(r.effects._edge_still_ours(a, w))


if __name__ == "__main__":
    unittest.main()

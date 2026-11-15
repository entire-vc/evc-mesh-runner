"""The next-action prep wake must not feed on its own card-wait edge churn.

Mesh folds every edge id into the next-action generation (evc-mesh internal/domain/next_action.go,
``generation``). Since the typed card wait owns its edge (0.2.51) the loop was: wait_for on=card makes
edge E1 -> new generation -> ``_wake_prep_generation`` wakes -> the wake end removes E1 -> the agent
re-declares the same wait -> edge E2 -> new generation -> wake, every 1-2 minutes with nothing new.

The blocker's HEAD and review verdict are in the generation too, so every push and every SHIP on the
blocker woke the waiting card once more with nothing to act on.

Now only the projection's content counts: a generation with the recorded content (new edge ids, a
blocker push or verdict that releases nothing) moves the mark without a wake; a change of the content
(released, reason, gates, targets, conditions, prep readiness) still wakes once.

FakeMesh is the external Mesh boundary, with a projection whose generation hashes the edge ids the way
Mesh does; Runner, Store and Effects are real.
"""
import copy
import hashlib
import json
import unittest

from fakes import AGENT_ID, task
from mesh_runner import next_action
from test_card_wait_owned_edge import OwnerCheckedMesh
from test_next_action_prep import BLK, EDGE, PROJ, TID, Store
from test_typed_outcome import TypedTurn

EDGE2 = "0000bbbb-0000-4000-8000-000000000012"
BLK2 = "0000cccc-0000-4000-8000-000000000013"


def proj(generation, *rows, prep_ready=True, next_act="continue_prep"):
    """A Mesh projection whose requirement rows are (edge_id, from_task, condition, gates)."""
    requires = [{"edge_id": e, "from_task": b, "condition": c, "gates": list(g), "released": False}
                for e, b, c, g in rows]
    requires.sort(key=lambda r: r["edge_id"])                     # Mesh orders them by edge id
    return {"version": 1, "task_id": TID, "generation": generation, "next_action": next_act,
            "stages": [{"stage": "prep", "ready": prep_ready},
                       {"stage": "final_acceptance", "ready": False, "blocked_by": copy.deepcopy(requires)}],
            "requires": requires, "produces": []}


FINAL = ("final_acceptance",)


class Dedupe(unittest.TestCase):
    """dispatched(): which new generations are news."""

    def setUp(self):
        self.store = Store()
        next_action.record(self.store, "A", TID, "v1:g1", "att1", proj("v1:g1", (EDGE, BLK, "shipped", FINAL)))

    def mark(self):
        return json.loads(self.store.marks[("A", TID, next_action.MARK)][0])

    def news(self, p):
        return not next_action.dispatched(self.store, "A", TID, p["generation"], p)

    def test_redeclared_wait_with_a_new_edge_id_is_not_news(self):
        """AC1: same target and condition, new edge id, prep/gates/released unchanged: no wake."""
        p = proj("v1:g2", (EDGE2, BLK, "shipped", FINAL))
        self.assertFalse(self.news(p))
        rec = self.mark()
        self.assertEqual((rec["generation"], rec["attempt"], rec["edges"]), ("v1:g2", "att1", [EDGE2]))

    def test_blocker_push_or_verdict_under_the_same_edges_is_not_news(self):
        """The blocker's HEAD and verdict are in the generation, not in the content: no wake, the
        mark moves (same attempt, same content)."""
        before = self.mark()
        for g in ("v1:push1", "v1:ship1", "v1:push2"):
            with self.subTest(g):
                self.assertFalse(self.news(proj(g, (EDGE, BLK, "shipped", FINAL))))
                rec = self.mark()
                self.assertEqual((rec["generation"], rec["attempt"], rec["content"]), (g, "att1", before["content"]))
        self.assertFalse(self.news(proj("v1:g4", (EDGE2, BLK, "shipped", FINAL))))   # churn: re-baselined
        self.assertFalse(self.news(proj("v1:g5", (EDGE2, BLK, "shipped", FINAL))))   # then B pushes again

    def test_a_blocker_move_that_changes_the_content_still_wakes(self):
        """A SHIP or merge that releases the edge, or a verdict Mesh names as the reason it holds."""
        released = proj("v1:r1", (EDGE, BLK, "shipped", FINAL))
        released["requires"][0]["released"] = True
        held = proj("v1:r2", (EDGE, BLK, "shipped", FINAL))
        held["requires"][0]["reason"] = "review verdict is for another SHA than current HEAD"
        owner = proj("v1:r3", (EDGE, BLK, "shipped", FINAL))
        owner["requires"][0]["owner"] = "builder"
        for p in (released, held, owner):
            with self.subTest(p["generation"]):
                self.assertTrue(self.news(p))
                self.assertEqual(self.mark()["generation"], "v1:g1")

    def test_real_changes_wake_even_with_a_new_edge_id(self):
        """AC2: prep first ready, a gate shift, another target or condition, a released edge."""
        start = proj("v1:g0", (EDGE, BLK, "shipped", FINAL), prep_ready=False, next_act="wait_requirement")
        changed = [
            proj("v1:c1", (EDGE2, BLK, "shipped", FINAL)),                                    # prep first ready
            proj("v1:c2", (EDGE2, BLK, "shipped", ("prep", "final_acceptance"))),            # gates shift
            proj("v1:c3", (EDGE2, BLK2, "shipped", FINAL)),                                   # another target
            proj("v1:c4", (EDGE2, BLK, "deployed", FINAL)),                                   # another condition
            proj("v1:c5", (EDGE2, BLK, "shipped", FINAL), (EDGE, BLK2, "shipped", FINAL)),   # one more target
        ]
        released = proj("v1:c6", (EDGE2, BLK, "shipped", FINAL))
        released["requires"][0]["released"] = True
        changed.append(released)
        for p in changed:
            with self.subTest(p["generation"]):
                self.store = Store()
                base = start if p["generation"] == "v1:c1" else proj("v1:g1", (EDGE, BLK, "shipped", FINAL))
                next_action.record(self.store, "A", TID, base["generation"], "att1", base)
                self.assertTrue(self.news(p))
                self.assertEqual(self.mark()["generation"], base["generation"])   # only a real start/wake marks

    def test_row_order_and_blocked_by_ids_do_not_count(self):
        """Mesh sorts rows by edge id: new ids reorder them; the content is a multiset."""
        self.store = Store()
        two = proj("v1:t1", (EDGE, BLK, "shipped", FINAL), (EDGE2, BLK2, "deployed", FINAL))
        next_action.record(self.store, "A", TID, "v1:t1", "att1", two)
        swapped = proj("v1:t2", ("ffff0000-0000-4000-8000-000000000001", BLK, "shipped", FINAL),
                       ("00000000-0000-4000-8000-000000000001", BLK2, "deployed", FINAL))
        self.assertNotEqual([r["from_task"] for r in two["requires"]], [r["from_task"] for r in swapped["requires"]])
        self.assertFalse(self.news(swapped))

    def test_a_mark_without_content_compares_generations_only(self):
        """A mark 0.2.51 wrote ({generation, attempt}) wakes once on a new generation, as before."""
        self.store = Store()
        self.store.mark("A", TID, next_action.MARK, json.dumps({"generation": "v1:g1", "attempt": "att1"}))
        self.assertTrue(self.news(proj("v1:g2", (EDGE2, BLK, "shipped", FINAL))))
        self.assertFalse(next_action.dispatched(self.store, "A", TID, "v1:g2"))        # no projection: as before
        self.assertTrue(next_action.dispatched(self.store, "A", TID, "v1:g1"))

    def test_fingerprint_ignores_unknown_fields_and_survives_junk(self):
        p = proj("v1:g1", (EDGE, BLK, "shipped", FINAL))
        q = copy.deepcopy(p)
        q["future_field"] = "edge-xyz"
        q["requires"][0]["wait_id"] = "w-9"
        self.assertEqual(next_action.fingerprint(p), next_action.fingerprint(q))
        for junk in (None, [], {}, {"requires": "x", "stages": [1, None]}, {"requires": [None, {"edge_id": []}]}):
            next_action.fingerprint(junk)                                  # never raises


def mesh_projection(mesh, source, heads):
    """GET /tasks/:id/next-action as evc-mesh builds it: requires from the open blocks edges, the
    generation a hash over each edge's id, target, condition and the blocker's HEAD."""
    edges = [e for e in mesh.deps.get(source["id"], []) if e.get("dependency_type") == "blocks"
             and not e.get("condition_satisfied")]
    lines = sorted(f"in|{e['id']}|{e['depends_on_task_id']}|{e['condition']}|{heads.get(e['depends_on_task_id'], '')}"
                   for e in edges)
    generation = "v1:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    requires = sorted(({"edge_id": e["id"], "from_task": e["depends_on_task_id"], "condition": e["condition"],
                        "gates": ["final_acceptance"] if e["condition"] in ("shipped", "deployed", "accepted")
                        else ["prep", "final_acceptance"], "released": False} for e in edges),
                      key=lambda r: r["edge_id"])
    prep_ready = all(r["gates"] == ["final_acceptance"] for r in requires)
    return {"version": 1, "task_id": source["id"], "generation": generation,
            "next_action": "continue_prep" if prep_ready else "wait_requirement",
            "stages": [{"stage": "prep", "ready": prep_ready},
                       {"stage": "final_acceptance", "ready": not requires, "blocked_by": requires}],
            "requires": requires, "produces": []}


class EdgeChurnLoop(TypedTurn):
    """The live loop (seen on a lane from 0.2.51 on) through the real wake, cleanup and settle."""

    def setUp(self):
        super().setUp()
        self.agent.next_action_prep = True
        self.heads = {}

    def declare(self, mesh, source, target, wait_id, edge_id, condition="shipped"):
        """What Mesh does on wait_for on=card: a new owned edge (linkWaitTarget) and the typed outcome."""
        mesh.deps[source["id"]] = [{"id": edge_id, "task_id": source["id"], "depends_on_task_id": target["id"],
                                    "dependency_type": "blocks", "condition": condition,
                                    "condition_satisfied": False, "generation": 1, "owner_wait_id": wait_id}]
        wait = {"id": wait_id, "kind": "card", "target_task_id": target["id"], "target_condition": condition,
                "dependency_id": edge_id, "dependency_generation": 1, "dependency_owned": True}
        mesh.waits = {source["id"]: wait}
        mesh.outcomes = {source["id"]: [{"id": "o-" + wait_id, "task_id": source["id"], "agent_id": AGENT_ID,
                                         "kind": "wait", "wait_id": wait_id, "note": "",
                                         "created_at": "2099-01-01T00:00:00+00:00"}]}
        mesh.checkouts.pop(source["id"], None)                  # wait_for releases the checkout

    def turn(self, r, mesh, source, target, wait_id, edge_id):
        """The woken attempt resumes (its wake end removes the old owned edge) and re-declares."""
        r.tick()
        self.declare(mesh, source, target, wait_id, edge_id)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["mesh_wait_id"], a.wait["edge"]), ("waiting", wait_id, edge_id))
        return a

    def prep_wakes(self, r, a):
        return [d for _, k, d in r.store.events(a.id) if k == "next_action_prep"]

    def test_redeclared_card_wait_does_not_wake_itself_and_real_changes_still_do(self):
        source, target = task(1), task(3, assignee_id="b-owner")      # B belongs to another lane
        mesh = OwnerCheckedMesh([source, target])
        mesh.next_action = lambda tid: mesh_projection(mesh, source, self.heads)
        r = self.run_turn(mesh, source)
        self.declare(mesh, source, target, "w-1", "e-1")
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["edge"]), ("waiting", "e-1"))

        # prep first ready under this wait: one wake (unchanged behaviour)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(len(self.prep_wakes(r, a)), 1)

        # AC1: the wake end removes e-1, the agent re-declares the same wait -> e-2, a new generation
        a = self.turn(r, mesh, source, target, "w-2", "e-2")
        self.assertIn((source["id"], "e-1", 1, "w-1"), mesh.wait_deletes)          # the churn really happened
        g2 = mesh_projection(mesh, source, self.heads)["generation"]
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")                        # 0.2.51: woken here
        self.assertEqual(len(self.prep_wakes(r, a)), 1)
        mark = json.loads(r.store.mark(self.agent.name, source["id"], next_action.MARK)[0])
        self.assertEqual((mark["generation"], mark["attempt"]), (g2, a.id))         # re-baselined, same attempt
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")

        # the blocker pushes (its HEAD is in Mesh's generation): no wake, the mark follows
        self.heads[target["id"]] = "b6d82f43"
        g3 = mesh_projection(mesh, source, self.heads)["generation"]
        self.assertNotEqual(g3, g2)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")                        # 0.2.53: woken here
        self.assertEqual(len(self.prep_wakes(r, a)), 1)
        mark = json.loads(r.store.mark(self.agent.name, source["id"], next_action.MARK)[0])
        self.assertEqual((mark["generation"], mark["attempt"]), (g3, a.id))

        # the projection's content changes (another blocker joins under a final-only gate): one wake
        mesh.deps[source["id"]].append({"id": "e-x", "task_id": source["id"], "depends_on_task_id": task(4)["id"],
                                        "dependency_type": "blocks", "condition": "deployed",
                                        "condition_satisfied": False})
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(len(self.prep_wakes(r, a)), 2)

        # and after that real wake the next re-declaration is quiet again
        a = self.turn(r, mesh, source, target, "w-3", "e-3")
        mesh.deps[source["id"]].append({"id": "e-x", "task_id": source["id"], "depends_on_task_id": task(4)["id"],
                                        "dependency_type": "blocks", "condition": "deployed",
                                        "condition_satisfied": False})
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(len(self.prep_wakes(r, a)), 2)

    def test_pushes_and_verdicts_on_the_blocker_do_not_wake_the_waiting_card(self):
        """The live probe: one card wait on a blocker another lane keeps pushing and reviewing. Each
        push and each SHIP is a new Mesh generation; none of them may wake the waiting card."""
        source, target = task(1), task(3, assignee_id="b-owner")
        mesh = OwnerCheckedMesh([source, target])
        mesh.next_action = lambda tid: mesh_projection(mesh, source, self.heads)
        r = self.run_turn(mesh, source)
        self.declare(mesh, source, target, "w-1", "e-1")
        a = self.finish_turn(r)
        r._wake_one(a)                                                     # the first prep: one wake
        self.assertEqual(len(self.prep_wakes(r, a)), 1)
        a = self.turn(r, mesh, source, target, "w-2", "e-2")
        r._wake_one(a)                                                     # the re-declaration: quiet
        self.assertEqual(r.store.get(a.id).state, "waiting")
        seen = set()
        for i, head in enumerate(("2ad5bda7", "SHIP@2ad5bda7", "c1aca273", "SHIP@c1aca273", "1dd36d6a")):
            self.heads[target["id"]] = head
            seen.add(mesh_projection(mesh, source, self.heads)["generation"])
            r._wake_one(a)
            with self.subTest(i=i, head=head):
                self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(len(seen), 5)                                     # five new generations
        self.assertEqual(len(self.prep_wakes(r, a)), 1)                    # 0.2.53: woken by the first push


if __name__ == "__main__":
    unittest.main()

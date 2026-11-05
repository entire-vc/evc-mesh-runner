"""Satisfied formal edges must not hide an addressed handback behind an old WAIT."""
import copy
import datetime as dt
import unittest

from mesh_runner.card_wait_reconcile import blocks_stale_observation, sole_dependency
from mesh_runner.store import iso
from fakes import FakeMesh, task
from test_runner import Bench

TID = "00000000-0000-0000-0000-000000000001"
CONDITIONAL = ("access_ready", "contract_ready", "artifact_published",
               "shipped", "deployed", "accepted")

def edge(n, tid=TID, satisfied=True):
    return {"id": f"edge-{n}", "task_id": tid,
            "depends_on_task_id": f"{n:08x}-0000-0000-0000-000000000002",
            "dependency_type": "blocks", "condition": "task_done",
            "condition_satisfied": satisfied}

class Predicate(unittest.TestCase):
    def test_informational_link_does_not_hide_current_card_observer(self):
        # A live publisher had this sole outgoing link to an unrelated card.
        for satisfied in (False, True):
            link = dict(edge(1, satisfied=satisfied), dependency_type="relates_to")
            with self.subTest(satisfied=satisfied):
                self.assertFalse(blocks_stale_observation({"outgoing": [link]}, TID, "deadbeef"))
                # Ignoring a link for observation never authorizes retargeting.
                self.assertIsNone(sole_dependency({"outgoing": [link]}, TID, "00000001"))

    def test_links_preserve_formal_dependency_authority(self):
        link = dict(edge(1), dependency_type="relates_to")
        cases = (([link, edge(2)], "deadbeef", False),
                 ([link, edge(2, satisfied=False)], "deadbeef", True),
                 ([link, edge(2, satisfied=False)], "00000002", False),
                 ([link, edge(2, satisfied=False), edge(3, satisfied=False)], "00000002", True))
        for edges, target, blocked in cases:
            with self.subTest(edges=edges, target=target):
                self.assertEqual(blocks_stale_observation({"outgoing": edges}, TID, target), blocked)

    def test_unknown_foreign_truncated_or_duplicate_links_hold(self):
        link = dict(edge(1), dependency_type="relates_to")
        cases = []
        for key, value in (("task_id", "foreign"), ("dependency_type", "unknown"),
                           ("condition", "unknown"), ("condition_satisfied", 1),
                           ("depends_on_task_id", "bad"), ("depends_on_task_id", TID),
                           ("id", ""), ("id", [])):
            cases.append({"outgoing": [dict(link, **{key: value})]})
        for key in link:
            malformed = dict(link); del malformed[key]
            cases.append({"outgoing": [malformed]})
        cases += [{"outgoing": [link], "has_more": True},
                  {"outgoing": [link], "next": "page2"},
                  {"outgoing": [link, None]}, {"outgoing": [link, link]},
                  {"outgoing": [link, dict(link, id="another-row")]},
                  {"outgoing": [link, dict(edge(2), id=link["id"])]}]
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertTrue(blocks_stale_observation(rows, TID, "deadbeef"))

    def test_seven_satisfied_edges_allow_observer_even_for_retired_target(self):
        self.assertFalse(blocks_stale_observation({"outgoing": [edge(n) for n in range(1, 8)]}, TID, "deadbeef"))

    def test_single_satisfied_other_target_also_allows_observer(self):
        self.assertFalse(blocks_stale_observation({"outgoing": [edge(1)]}, TID, "deadbeef"))

    def test_server_satisfied_conditional_edges_allow_observer_without_retargeting(self):
        for condition in CONDITIONAL:
            fulfilled = dict(edge(1), condition=condition)
            with self.subTest(condition=condition):
                self.assertFalse(blocks_stale_observation({"outgoing": [fulfilled]}, TID, "deadbeef"))
                self.assertIsNone(sole_dependency({"outgoing": [fulfilled]}, TID,
                                                  "00000001", allow_satisfied=True))
        mixed = [dict(edge(n), condition=condition)
                 for n, condition in enumerate(("task_done",) + CONDITIONAL, 1)]
        self.assertFalse(blocks_stale_observation({"outgoing": mixed}, TID, "deadbeef"))

    def test_conditional_edges_require_an_explicit_true_verdict(self):
        for condition in CONDITIONAL:
            for verdict in (False, None, 0, 1, "true"):
                for target in ("deadbeef", "00000001"):
                    e = dict(edge(1), condition=condition, condition_satisfied=verdict)
                    with self.subTest(condition=condition, verdict=verdict, target=target):
                        self.assertTrue(blocks_stale_observation({"outgoing": [e]}, TID, target))
            missing = dict(edge(1), condition=condition)
            del missing["condition_satisfied"]
            self.assertTrue(blocks_stale_observation({"outgoing": [missing]}, TID, "00000001"))

    def test_conditional_informational_links_are_invalid_even_when_satisfied(self):
        # Mesh permits conditional gates only on blocks, never informational links.
        for condition in CONDITIONAL:
            malformed = dict(edge(1), dependency_type="relates_to", condition=condition)
            with self.subTest(condition=condition):
                self.assertTrue(blocks_stale_observation({"outgoing": [malformed]}, TID, "deadbeef"))

    def test_satisfied_condition_cannot_hide_invalid_or_truncated_edges(self):
        fulfilled = dict(edge(1), condition="deployed")
        for key, value in (("id", ""), ("task_id", "foreign"),
                           ("depends_on_task_id", "bad"), ("condition", []),
                           ("condition", {}), ("condition", "future_condition")):
            with self.subTest(key=key, value=value):
                self.assertTrue(blocks_stale_observation(
                    {"outgoing": [dict(fulfilled, **{key: value})]}, TID, "00000001"))
        for rows in ({"outgoing": [fulfilled], "has_more": True},
                     {"outgoing": [fulfilled], "next": "page2"},
                     {"outgoing": [fulfilled, fulfilled]},
                     {"outgoing": [fulfilled, dict(fulfilled, id="duplicate")]},
                     {"outgoing": [fulfilled, dict(edge(2), condition="accepted",
                                                   condition_satisfied=False)]}):
            with self.subTest(rows=rows):
                self.assertTrue(blocks_stale_observation(rows, TID, "00000001"))

    def test_semantic_duplicate_with_distinct_row_identity_still_holds(self):
        first = edge(1)
        duplicate = dict(first, id="different-row")
        for rows in ([first, duplicate], [duplicate, first]):
            with self.subTest(rows=rows):
                self.assertTrue(blocks_stale_observation({"outgoing": rows}, TID, "deadbeef"))

    def test_unknown_or_ambiguous_edges_still_hold(self):
        base = {"outgoing": [edge(1), edge(2)]}
        cases = []
        for key, value in (("id", ""), ("id", []), ("id", {}), ("id", True), ("task_id", "other"), ("dependency_type", "relates"),
                           ("condition", "unknown"), ("condition_satisfied", False),
                           ("condition_satisfied", None), ("condition_satisfied", 1),
                           ("depends_on_task_id", "bad"), ("depends_on_task_id", TID)):
            d = copy.deepcopy(base); d["outgoing"][1][key] = value; cases.append(d)
        for key in ("id", "task_id", "condition", "condition_satisfied"):
            d = copy.deepcopy(base); del d["outgoing"][1][key]; cases.append(d)
        cases += [{"outgoing": [edge(1), None]}, {"outgoing": [edge(1), edge(1)]},
                  dict(base, has_more=True), dict(base, next="page2"), {}, {"outgoing": None}]
        for d in cases:
            with self.subTest(rows=d):
                self.assertTrue(blocks_stale_observation(d, TID, "deadbeef"))

    def test_unknown_single_edge_identity_cannot_use_current_target_exception(self):
        for value in (None, True, [], {}, "", "   "):
            e = edge(1); e["id"] = value
            with self.subTest(identity=value):
                self.assertTrue(blocks_stale_observation({"outgoing": [e]}, TID, "00000001"))

    def test_existing_single_current_unsatisfied_edge_contract(self):
        self.assertFalse(blocks_stale_observation({"outgoing": [edge(1, satisfied=False)]}, TID, "00000001"))
        self.assertTrue(blocks_stale_observation({"outgoing": [edge(1, satisfied=False)]}, TID, "deadbeef"))
        self.assertFalse(blocks_stale_observation({"outgoing": []}, TID, "deadbeef"))

class SameAttemptWake(Bench):
    def prepare(self, body="@builder continue the remaining delivery", satisfied=True):
        self.cfg.comment_wake = "addressed"
        t, awaited = task(1), task(2)
        # Mesh is the external HTTP boundary; Store, reconciliation and wake/CAS are real.
        mesh = FakeMesh([t, awaited]); r = self.runner(mesh); r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait={"kind": "card", "target": awaited["id"][:8],
            "since": iso(), "started": iso(), "until": "", "handle": ""})
        mesh.deps[t["id"]] = [edge(n, t["id"], satisfied) for n in range(10, 17)]
        c = mesh.add_comment(t["id"], body, author="foreign-lead",
            at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        c["author_type"] = "agent"
        return r, a

    def test_done_card_with_informational_link_wakes_same_attempt_once(self):
        r, a = self.prepare(body="FYI progress only")
        r.mesh.tasks[task(2)["id"]]["status_id"] = "s-done"
        r.mesh.deps[a.task_id] = [dict(edge(1, a.task_id), dependency_type="relates_to")]
        r.store.update(a, session_id="preserved-session")
        a = r.store.get(a.id)
        original_wait = copy.deepcopy(a.wait)
        original_edges = copy.deepcopy(r.mesh.deps)
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.task_id, b.session_id, b.turns, b.state),
                         (a.id, a.task_id, a.session_id, a.turns, "ready"))
        self.assertIn("done", b.wake_text)
        r._wake_one(a)  # replay of the stale waiting snapshot cannot deliver twice
        current = r.store.get(a.id)
        self.assertEqual((current.wait, current.wake_text), (b.wait, b.wake_text))
        self.assertEqual(len(r.store.by_state(a.agent, ("ready", "waiting"))), 1)
        self.assertEqual(r.mesh.deps, original_edges)
        self.assertEqual(b.wait["target"], original_wait["target"])
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())

    def test_informational_link_alone_is_not_a_wake_event(self):
        r, a = self.prepare(body="FYI progress only")
        r.mesh.deps[a.task_id] = [dict(edge(1, a.task_id), dependency_type="relates_to")]
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())

    def test_done_card_with_fulfilled_deployed_edge_wakes_same_attempt_once(self):
        r, a = self.prepare(body="FYI progress only")
        awaited = r.mesh.tasks[task(2)["id"]]
        awaited["status_id"] = "s-done"
        r.mesh.deps[a.task_id] = [dict(edge(2, a.task_id), condition="deployed",
            depends_on_task_id=awaited["id"],
            evidence_url="https://git.example/jobs/566637")]
        r.store.update(a, session_id="preserved-session")
        a = r.store.get(a.id)
        original_edges = copy.deepcopy(r.mesh.deps)
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.task_id, b.session_id, b.turns, b.state),
                         (a.id, a.task_id, a.session_id, a.turns, "ready"))
        self.assertIn("done", b.wake_text)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).wake_text, b.wake_text)
        self.assertEqual(len(r.store.by_state(a.agent, ("ready", "waiting"))), 1)
        self.assertEqual(r.mesh.deps, original_edges)
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())

    def test_done_card_does_not_bypass_unfulfilled_deployment(self):
        r, a = self.prepare(body="@builder continue the remaining delivery")
        r.mesh.tasks[task(2)["id"]]["status_id"] = "s-done"
        r.mesh.deps[a.task_id] = [dict(edge(2, a.task_id, satisfied=False),
            condition="deployed", depends_on_task_id=task(2)["id"])]
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())

    def test_fulfilled_deployment_alone_is_not_a_wake_event(self):
        r, a = self.prepare(body="FYI progress only")
        r.mesh.deps[a.task_id] = [dict(edge(2, a.task_id), condition="deployed",
            depends_on_task_id=task(2)["id"])]
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())

    def test_addressed_foreign_handback_wakes_same_attempt_once_without_spawn(self):
        r, a = self.prepare(); original_wait = copy.deepcopy(a.wait)
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.task_id, b.agent, b.state), (a.id, a.task_id, a.agent, "ready"))
        self.assertIn("continue the remaining delivery", b.wake_text)
        self.assertEqual(b.turns, a.turns)
        self.assertFalse(b.pid)
        self.assertEqual(r.store.by_state(a.agent, ("ready", "waiting"))[0].id, a.id)
        self.assertEqual(len(r.store.by_state(a.agent, ("ready", "waiting"))), 1)
        self.assertFalse(r.procs)
        self.assertFalse(self.calls.exists())
        self.assertEqual(b.wait, original_wait)  # retain the parked checkpoint
        r._wake_one(a)  # a stale observer cannot overwrite the READY CAS
        self.assertEqual(r.store.get(a.id).wake_text, b.wake_text)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertFalse(r.procs)

    def test_satisfied_edges_do_not_wake_for_unaddressed_progress(self):
        r, a = self.prepare(body="FYI progress only")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)

    def test_addressed_comment_cannot_bypass_ambiguous_unsatisfied_edges(self):
        r, a = self.prepare(satisfied=False)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)

if __name__ == "__main__":
    unittest.main()

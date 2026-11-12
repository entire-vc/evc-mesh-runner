"""Producer side of the operational-constraints contract (additive v1): the six classes,
observation times, and that publishing constraints changes no wait behaviour."""
import datetime as dt
import unittest
from unittest import mock

from mesh_runner.effects import Effects, runtime_state, skip_state, wait_constraints
from mesh_runner.mesh import MeshError
from mesh_runner.store import Attempt, iso, now

SINCE = "2026-11-03T10:00:00+00:00"


def waiting(kind, target, **wait):
    return Attempt(id="a1", agent="lane", task_id="1" * 32, harness="x", state="waiting",
                   wait={"kind": kind, "target": target, "since": SINCE, **wait})


class FakeMesh:
    def __init__(self, refuse_constraints=False):
        self.puts, self.refuse = [], refuse_constraints

    def put_runtime_state(self, tid, state):
        if self.refuse and "constraints" in state:
            raise MeshError(422, "invalid runtime_state: constraints[0].class is bad", {"field": "constraints[0].class"})
        self.puts.append(state)


class ProducerSixClasses(unittest.TestCase):
    """Semantic fixture: every class the API accepts has a producer path, and a dependency stays the server's."""

    def classes(self):
        out = {}
        for a in (waiting("pipeline", "g/p#7", seen={"pipeline": "manual"}),):
            for c in runtime_state(a, "lane")["constraints"]:
                out[c["class"]] = c
        for reason in ("review: already reviewed (DO-NOT-SHIP); waits for a new comment",
                       "capability held; waits for authenticated own-access recheck",
                       "dependencies unreadable (403)"):
            for c in skip_state(reason, "lane")["constraints"]:
                out[c["class"]] = c
        return out

    def test_five_producer_classes_present(self):
        got = self.classes()
        self.assertEqual(set(got), {"local_wait", "ci_capacity", "review_rejection", "expired_authority", "write_acl"})

    def test_mesh_dependency_is_left_to_the_server(self):
        # negative control: a plain blocker hold and a card wait emit no mesh_dependency
        self.assertNotIn("constraints", skip_state("blocked by abcdef12, 12345678", "lane"))
        classes = [c["class"] for c in runtime_state(waiting("card", "abcdef12"), "lane")["constraints"]]
        self.assertEqual(classes, ["local_wait"])

    def test_plain_reviewed_hold_is_not_a_rejection(self):
        self.assertNotIn("constraints", skip_state("review: already reviewed; waits for a new comment", "lane"))

    def test_every_constraint_has_the_required_contract_fields(self):
        for c in self.classes().values():
            self.assertTrue(c["id"] and c["reason_code"] and c["wake"]["predicate"] and c["source"]["kind"])
            self.assertIn(c["next_actor"]["kind"], ("agent", "human", "unknown"))


class WaitConstraint(unittest.TestCase):
    def test_pipeline_wait_copies_the_existing_event(self):
        a = waiting("pipeline", "grp/proj#42", ci_change={"predicate": "success", "jobs": ["build"], "sha": "x", "pipeline": 42, "status": "running"})
        (c,) = wait_constraints(a)
        self.assertEqual(c["class"], "local_wait")
        self.assertEqual((c["wake"]["predicate"], c["wake"]["ref"]), ("pipeline", "grp/proj#42"))
        self.assertEqual(c["wake"]["target"], {"provider": "gitlab", "project_path": "grp/proj", "pipeline_id": 42,
                                               "until": "success", "required_jobs": ["build"]})
        self.assertEqual(c["observed_at"], SINCE)        # declaration time, not publish time

    def test_mr_wait_target_and_decision_actor(self):
        (c,) = wait_constraints(waiting("mr", "g/p!5"))
        self.assertEqual(c["wake"]["target"]["mr_iid"], 5)
        (d,) = wait_constraints(waiting("decision", "q", handle="foreman"))
        self.assertEqual(d["next_actor"], {"kind": "agent", "name": "foreman"})
        (u,) = wait_constraints(waiting("card", "abcdef12"))
        self.assertEqual(u["next_actor"], {"kind": "unknown"})   # a card owner is not proven

    def test_ci_states_are_distinguished_and_carry_no_invented_scope(self):
        manual = wait_constraints(waiting("pipeline", "g/p#1", seen={"pipeline": "manual"}))[1]
        cap = wait_constraints(waiting("mr", "g/p!2", seen={"mr_pipeline": "9:waiting_for_resource"}))[1]
        self.assertEqual((manual["ci"]["state"], cap["ci"]["state"]), ("manual_required", "capacity"))
        for c in (manual, cap):
            self.assertNotIn("scope", c["ci"])
            self.assertNotIn("position", c["ci"])
        # negative control: a running pipeline is no CI constraint
        self.assertEqual(len(wait_constraints(waiting("pipeline", "g/p#1", seen={"pipeline": "running"}))), 1)

    def test_lane_queue_position_never_becomes_ci_position(self):
        q = runtime_state(Attempt(id="a", agent="l", task_id="t", harness="x", state="ready"), "lane")
        self.assertNotIn("constraints", q)

    def test_long_ids_stay_within_the_bound(self):
        (c,) = wait_constraints(waiting("pipeline", "g/" + "p" * 300 + "#1"))
        self.assertLessEqual(len(c["id"]), 128)


class Publishing(unittest.TestCase):
    def test_observation_time_kept_or_stamped(self):
        mesh = FakeMesh()
        fx = Effects(mesh, None, "lane")
        fx._put("t", {"kind": "waiting", "constraints": [
            {"id": "own", "observed_at": SINCE}, {"id": "now"}]})
        got = {c["id"]: c["observed_at"] for c in mesh.puts[0]["constraints"]}
        self.assertEqual(got["own"], SINCE, "a heartbeat must not freshen a recorded observation")
        self.assertNotEqual(got["now"], SINCE)
        self.assertLess(abs((dt.datetime.fromisoformat(got["now"]) - now()).total_seconds()), 5)

    def test_refused_constraints_do_not_block_the_base_state(self):
        mesh = FakeMesh(refuse_constraints=True)
        Effects(mesh, None, "lane")._put("t", {"kind": "waiting", "ref": "time", "constraints": [{"id": "x"}]})
        self.assertEqual(mesh.puts, [{"kind": "waiting", "ref": "time"}])

    def test_other_refusals_still_raise(self):
        class Bad(FakeMesh):
            def put_runtime_state(self, tid, state):
                raise MeshError(503, "down")
        with self.assertRaises(MeshError):
            Effects(Bad(), None, "lane")._put("t", {"kind": "waiting", "constraints": [{"id": "x"}]})

    def test_unchanged_state_is_not_rewritten_before_the_heartbeat(self):
        mesh = FakeMesh()
        fx = Effects(mesh, None, "lane")
        a = waiting("pipeline", "g/p#7", seen={"pipeline": "manual"})
        fx.publish([a])
        fx.publish([a])
        self.assertEqual(len(mesh.puts), 1, "constraints are stable in the comparison; no churn per pass")


if __name__ == "__main__":
    unittest.main()

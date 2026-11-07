"""A human-gated card is admitted to its ASK OWNER for one turn per answer - nobody else, never twice."""
from fakes import AGENT_ID, FakeMesh, task
from test_runner import Bench
from mesh_runner import gate_owner


def info(owner=AGENT_ID, pending=True, did="c0000001-0000-4000-8000-000000000001"):
    return {"gated": True, "owner_agent_id": owner, "decision_pending": pending,
            "decision_comment_id": did if pending else None}


class PendingDecision(Bench):
    def test_only_the_owner_with_a_pending_answer_is_admitted(self):
        t = task(1, human_gate=True, human_gate_info=info())
        self.assertEqual(gate_owner.pending_decision(t, AGENT_ID), "c0000001-0000-4000-8000-000000000001")
        # negative controls, one defect each
        self.assertEqual(gate_owner.pending_decision(t, "someone-else"), "")
        self.assertEqual(gate_owner.pending_decision(dict(t, human_gate_info=info(owner="other")), AGENT_ID), "")
        self.assertEqual(gate_owner.pending_decision(dict(t, human_gate_info=info(pending=False)), AGENT_ID), "")
        self.assertEqual(gate_owner.pending_decision(dict(t, human_gate=False), AGENT_ID), "")
        self.assertEqual(gate_owner.pending_decision(dict(t, assignee_id="x"), AGENT_ID), "")
        self.assertEqual(gate_owner.pending_decision(dict(t, human_gate_info=None), AGENT_ID), "")


class Admission(Bench):
    def runner_for(self, **extra):
        t = task(1, human_gate=True, **extra)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t

    def test_gated_card_without_a_decision_stays_held(self):
        r, t = self.runner_for(human_gate_info=info(pending=False))
        self.assertEqual(r._gate_decision_for(t), "")
        self.assertEqual([c for c in r.candidates() if c.get("_gate_decision")], [])

    def test_owner_is_admitted_once_per_decision(self):
        r, t = self.runner_for(human_gate_info=info())
        got = [c for c in r.candidates() if c.get("_gate_decision")]
        self.assertEqual(len(got), 1)
        a = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), "c0000001-0000-4000-8000-000000000001")
        r.store.update(a, turns=1, state="done")
        self.assertEqual(r._gate_decision_for(t, consume=True), "", "the same answer must not admit a second turn")
        self.assertEqual([c for c in r.candidates() if c.get("_gate_decision")], [])

    def test_a_new_answer_admits_again(self):
        r, t = self.runner_for(human_gate_info=info())
        self.assertTrue(r._gate_decision_for(t, consume=True))
        r.mesh.tasks[t["id"]]["human_gate_info"] = info(did="c0000002-0000-4000-8000-000000000002")
        self.assertEqual(r._gate_decision_for(t, consume=True), "c0000002-0000-4000-8000-000000000002")

    def test_foreign_owner_is_never_admitted(self):
        r, t = self.runner_for(human_gate_info=info(owner="agent-lead"))
        self.assertEqual(r._gate_decision_for(t, consume=True), "")


class ResumeConsumes(Bench):
    def test_resume_claims_the_decision_so_it_admits_one_resume_only(self):
        t = task(1, human_gate=True, human_gate_info=info())
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        # what _resume_one's gate check does: first call claims, a later resume is held
        a = r.store.create(r.agent.name, t["id"], "claude")
        self.assertTrue(r._gate_decision_for(t, consume=True, attempt_id=a.id))
        r.store.update(a, turns=1, state="done")
        b = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=b.id), "")


class ClaimSurvivesAbortedStart(Admission):
    test_gated_card_without_a_decision_stays_held = test_owner_is_admitted_once_per_decision = None
    test_a_new_answer_admits_again = test_foreign_owner_is_never_admitted = None
    DID = "c0000001-0000-4000-8000-000000000001"

    def test_aborted_before_launch_leaves_the_answer_claimable(self):
        r, t = self.runner_for(human_gate_info=info())
        a1 = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a1.id), self.DID)
        # a1 aborted before any turn (workspace pool full): the answer must not be lost
        r.store.update(a1, state="aborted")
        a2 = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a2.id), self.DID)

    def test_launched_attempt_spends_the_answer(self):
        r, t = self.runner_for(human_gate_info=info())
        a1 = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a1.id), self.DID)
        r.store.update(a1, turns=1, state="done")
        a2 = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a2.id), "",
                         "an answer that already had its turn must not admit another")


class QuotaWaitResumeAdmitted(Admission):
    test_gated_card_without_a_decision_stays_held = test_owner_is_admitted_once_per_decision = None
    test_a_new_answer_admits_again = test_foreign_owner_is_never_admitted = None

    def _reason(self, r, t, a):
        # mirrors the quota_wait_rejected branch of _resume_one
        from mesh_runner.selection import static_skip_reason
        from mesh_runner.runner import now
        plain = dict(t)
        did = r._gate_decision_for(plain, consume=True, attempt_id=a.id)
        if did:
            plain["_gate_decision"] = did
        return static_skip_reason(plain, r.agent.policy, now())

    def test_answered_gated_card_is_not_held_by_the_quota_wait_policy_check(self):
        r, t = self.runner_for(human_gate_info=info())
        r.agent.policy.human_gate = True
        a = r.store.create(r.agent.name, t["id"], "claude")
        self.assertNotEqual(self._reason(r, t, a), "waiting for a human decision")

    def test_unanswered_gated_card_stays_held_there(self):
        r, t = self.runner_for(human_gate_info=info(pending=False))
        r.agent.policy.human_gate = True
        a = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(self._reason(r, t, a), "waiting for a human decision")


class SameAttemptRetries(Admission):
    test_gated_card_without_a_decision_stays_held = test_owner_is_admitted_once_per_decision = None
    test_a_new_answer_admits_again = test_foreign_owner_is_never_admitted = None

    def test_deferred_resume_can_reclaim_its_own_answer(self):
        r, t = self.runner_for(human_gate_info=info())
        a = r.store.create(r.agent.name, t["id"], "claude")
        did = "c0000001-0000-4000-8000-000000000001"
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), did)
        # resume deferred before launching a turn; the same attempt retries
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), did)
        r.store.update(a, turns=1, state="done")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), "",
                         "once the turn ran the answer is spent for this attempt too")


class ResumedAttemptWithEarlierTurns(Admission):
    test_gated_card_without_a_decision_stays_held = test_owner_is_admitted_once_per_decision = None
    test_a_new_answer_admits_again = test_foreign_owner_is_never_admitted = None
    DID = "c0000001-0000-4000-8000-000000000001"

    def test_earlier_turns_do_not_spend_a_fresh_claim(self):
        r, t = self.runner_for(human_gate_info=info())
        a = r.store.create(r.agent.name, t["id"], "claude")
        r.store.update(a, turns=3)                      # a resumed attempt with history
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), self.DID)
        # resume deferred before launching: same attempt and a new one can both reclaim
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), self.DID)
        r.store.update(a, state="aborted")
        b = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=b.id), self.DID)
        r.store.update(b, turns=1, state="done")
        c = r.store.create(r.agent.name, t["id"], "claude")
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=c.id), "")

    def test_a_turn_after_the_claim_spends_it(self):
        r, t = self.runner_for(human_gate_info=info())
        a = r.store.create(r.agent.name, t["id"], "claude")
        r.store.update(a, turns=3)
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), self.DID)
        r.store.update(a, turns=4)
        self.assertEqual(r._gate_decision_for(t, consume=True, attempt_id=a.id), "")


class ScheduledOwnerTurn(Admission):
    """A future start_after must not strand the one admitted owner answer; nothing else gets a bypass."""
    test_gated_card_without_a_decision_stays_held = test_owner_is_admitted_once_per_decision = None
    test_a_new_answer_admits_again = test_foreign_owner_is_never_admitted = None
    FUTURE = "2099-01-01T00:00:00Z"

    def _reason(self, r, t, consume=False):
        from mesh_runner.selection import static_skip_reason
        from mesh_runner.runner import now
        plain = dict(t)
        a = r.store.create(r.agent.name, t["id"], "claude") if consume else None
        did = r._gate_decision_for(plain, consume=consume, attempt_id=a.id if a else "")
        if did:
            plain["_gate_decision"] = did
        return static_skip_reason(plain, r.agent.policy, now())

    def test_scheduled_card_with_an_owner_answer_is_admitted(self):
        r, t = self.runner_for(human_gate_info=info(), start_after=self.FUTURE)
        r.agent.policy.human_gate = True
        self.assertEqual(self._reason(r, t), "")
        got = [c for c in r.candidates() if c.get("_gate_decision")]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["start_after"], self.FUTURE, "the schedule itself is left alone")

    def test_scheduled_card_without_an_answer_stays_held(self):
        r, t = self.runner_for(human_gate_info=info(pending=False), start_after=self.FUTURE)
        r.agent.policy.human_gate = True
        self.assertEqual(self._reason(r, t), "waiting for a human decision")

    def test_foreign_owner_does_not_bypass_the_schedule(self):
        r, t = self.runner_for(human_gate_info=info(owner="agent-lead"), start_after=self.FUTURE)
        r.agent.policy.human_gate = True
        self.assertEqual(self._reason(r, t), "waiting for a human decision")

    def test_ordinary_scheduled_work_keeps_its_hold(self):
        from mesh_runner.selection import static_skip_reason
        from mesh_runner.runner import now
        r, t = self.runner_for(start_after=self.FUTURE)
        t = dict(t, human_gate=False)
        self.assertTrue(static_skip_reason(t, r.agent.policy, now()).startswith("start_after "))
        # a stray marker without a live gate is no bypass either
        t = dict(t, _gate_decision="c0000001-0000-4000-8000-000000000001")
        self.assertTrue(static_skip_reason(t, r.agent.policy, now()).startswith("start_after "))

    def test_spent_answer_on_a_scheduled_card_is_held_again(self):
        r, t = self.runner_for(human_gate_info=info(), start_after=self.FUTURE)
        r.agent.policy.human_gate = True
        a = r.store.create(r.agent.name, t["id"], "claude")
        self.assertTrue(r._gate_decision_for(t, consume=True, attempt_id=a.id))
        r.store.update(a, turns=1, state="done")
        self.assertEqual(self._reason(r, t, consume=True), "waiting for a human decision")

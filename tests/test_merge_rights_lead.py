"""A merge gate that passed but whose identity may not merge hands the merge to the lead.

Before: SHIP, pipeline success, the gate under a Developer of a protected main refused with exit 3
and woke the author, who cannot merge. Now the gate's last line ``⛔ MERGE RIGHTS`` makes
the runner file one merge-only card for the card's nearest lead run by this runner (the lead's own
runner merges under the lead's identity); the author keeps sleeping on the merge request.
"""
import dataclasses

from mesh_runner import reviewer_merge
from mesh_runner.mesh import MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from fakes import AgentView, task
from test_merge_wait import MergeBench, mr_row, OTHER, PROJECT, SHA, TARGET

RIGHTS = (f"  права на мёрж: эта GitLab-личность не может вливать в main\n"
          f"⛔ MERGE RIGHTS {TARGET} @{SHA}: this identity cannot merge into main; a maintainer must merge\n")


class LeadView(AgentView):
    """The fake Mesh through the lead's key: what the lead's runner writes is authored by the lead."""
    fail_comments = 0

    def add_comment(self, tid, body, author=None, at=None, key=""):
        if self.fail_comments:
            self.fail_comments -= 1
            raise MeshError(503, "unavailable")
        return self.mesh.add_comment(tid, body, author=self.agent_id, at=at, key=key)


class MergeRightsToLead(MergeBench):
    def setUp(self):
        super().setUp()
        # the lead is a lane of this runner (its own GitLab identity), and the project's lead
        self.cfg.agents = [self.agent, dataclasses.replace(self.agent, name="lead", key="k-lead",
                                                           gitlab_username=None, declared_env=None)]
        self.cfg.project_leads = {"p1": "lead"}

    def lead_runner(self, mesh, gl):
        view = LeadView(mesh, "agent-lead", "lead")
        lr = AgentRunner(self.cfg, self.cfg.agents[1], Store(self.root / "state" / "lead.sqlite"), mesh=view,
                         adapter=self.adapter, gitlab=gl)
        self.addCleanup(lr.store.db.close)
        return lr, view

    def handed(self):
        r, a, t, gl = self.waiting()
        self.say(RIGHTS, rc=4)
        a = self.check(r, a)                    # gate run 1
        a = self.check(r, a)                    # its result: handed to the lead
        return r, a, t, gl

    def test_rights_line_files_one_merge_only_card_for_the_lead_and_the_author_sleeps(self):
        r, a, t, gl = self.handed()
        self.assertEqual(a.state, "waiting")
        self.assertEqual(len(r.mesh.created), 1)
        card = r.mesh.created[0]
        self.assertEqual(r.mesh.tasks[card["id"]]["assignee_id"], "agent-lead")
        self.assertIn(reviewer_merge.LABEL, card["labels"])
        req = reviewer_merge.parse_request(card["description"])
        self.assertEqual((req["reviewer_lane"], req["reviewer_username"], req["project"], req["iid"], req["sha"],
                          req["source_task"], req["author_lane"], req["reason"]),
                         ("lead", "lead", PROJECT, "56", SHA, t["id"], "builder", reviewer_merge.RIGHTS_REASON))
        self.assertIn(f"wait_for on=merge project_path={PROJECT} mr_iid=56 after_sha={SHA}", card["description"])
        self.assertIn("лиду", card["description"])
        self.assertEqual(a.wait["merge"]["delegate"]["card"], card["id"])
        notes = [c["body"] for c in r.mesh.comment_log.get(t["id"], [])]
        self.assertTrue(any(f"merge-only card #{card['id'][:8]}" in n for n in notes), notes)
        self.assertFalse(any(n.removeprefix("[runner] ").startswith("@") for n in notes))   # no request card too
        # no more gate runs on the author's side, however long it waits
        for _ in range(3):
            a = self.check(r, a)
        self.assertEqual((a.state, len(self.runs()), len(r.mesh.created)), ("waiting", 1, 1))

    def test_the_author_wakes_when_the_lead_merged(self):
        r, a, t, gl = self.handed()
        gl.mr_rows[TARGET] = mr_row(state="merged", merge_commit_sha="c" * 40)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("merged at " + "c" * 40, a.wake_text)

    def test_the_lead_closing_its_card_without_the_merge_wakes_the_author(self):
        r, a, t, gl = self.handed()
        r.mesh.set_category(r.mesh.created[0]["id"], "done")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"merge-only card #{r.mesh.created[0]['id'][:8]} (lead)", a.wake_text)
        self.assertIn("still open", a.wake_text)

    def test_a_moved_head_drops_the_handover_and_gates_the_new_head(self):
        r, a, t, gl = self.handed()
        self.say("")                            # the new head: the gate is simply still open
        gl.mr_rows[TARGET] = mr_row(sha=OTHER)
        a = self.check(r, a)
        self.assertNotIn("delegate", a.wait["merge"])
        self.assertEqual(self.runs()[-1], f"56 {PROJECT} {OTHER}")

    def test_no_lead_lane_wakes_the_author_with_the_reason(self):
        self.cfg.project_leads = {}
        r, a, t, gl = self.waiting()
        self.say(RIGHTS, rc=4)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("no lead of this card is a lane of this runner", a.wake_text)
        self.assertIn("⛔ MERGE RIGHTS", a.wake_text)
        self.assertEqual(r.mesh.created, [])

    def test_a_lead_outside_this_runner_is_not_picked(self):
        self.cfg.agents = [self.agent]          # "lead" is a workspace agent, not a lane here
        r, a, t, gl = self.waiting()
        self.say(RIGHTS, rc=4)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertEqual(r.mesh.created, [])

    def test_the_line_of_another_mr_is_an_ordinary_refusal(self):
        r, a, t, gl = self.waiting()
        self.say(RIGHTS.replace(TARGET, PROJECT + "!57"), rc=4)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("failed (exit 4)", a.wake_text)
        self.assertEqual(r.mesh.created, [])

    def test_a_merge_only_card_is_not_handed_on(self):
        r, a, t, gl = self.waiting()
        r.mesh.tasks[t["id"]]["labels"] = [reviewer_merge.LABEL]
        text = r._merge_to_lead(a, {"target": TARGET}, {"sha": SHA}, (4, RIGHTS))
        self.assertIn("not handed on again", text)
        self.assertEqual(r.mesh.created, [])

    def test_a_create_failure_is_retried_then_told(self):
        r, a, t, gl = self.waiting()
        r.mesh.fail_create = 5
        m = {"sha": SHA}
        texts = [r._merge_to_lead(a, {"target": TARGET}, m, (4, RIGHTS)) for _ in range(3)]
        self.assertEqual(texts[:2], ["", ""])
        self.assertIn("could not be filed", texts[2])
        self.assertNotIn("delegate", m)

    def test_the_card_is_one_per_head_found_again_not_duplicated(self):
        r, a, t, gl = self.waiting()
        req = {"key": reviewer_merge.request_key(t["id"], PROJECT, "56", SHA), "source_task": t["id"],
               "project": PROJECT, "iid": "56", "sha": SHA, "reviewer_lane": "lead", "reviewer_username": "lead",
               "reason": reviewer_merge.RIGHTS_REASON, "author_lane": "builder", "author_family": ""}
        first = r._merge_only_card(r.mesh.tasks[t["id"]], req, "agent-lead")
        r.mesh.tasks[first]["assignee_id"] = None   # a create that died before its assign
        again = r._merge_only_card(r.mesh.tasks[t["id"]], req, "agent-lead")
        self.assertEqual((first, len(r.mesh.created)), (again, 1))
        self.assertEqual(r.mesh.tasks[first]["assignee_id"], "agent-lead")

    def test_the_lead_may_not_merge_either_wakes_the_author_too(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        lr, _ = self.lead_runner(r.mesh, gl)
        la = lr.store.create("lead", card, "claude")
        # the lead's own gate on the merge-only card: the same line, the lead is woken with it
        text = lr._merge_to_lead(la, {"target": TARGET}, {"sha": SHA}, (4, RIGHTS))
        self.assertIn("not handed on again", text)
        self.assertEqual(len(r.mesh.created), 1)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"merge-only card #{card[:8]} refused too", a.wake_text)
        self.assertIn("A maintainer of g/app must merge it", a.wake_text)

    def test_a_rights_note_not_from_the_lead_or_for_another_head_wakes_nobody(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        r.mesh.add_comment(card, f"[runner] merge gate: ⛔ MERGE RIGHTS {TARGET} @{SHA}: x", author="agent-other")
        r.mesh.add_comment(card, f"[runner] merge gate: ⛔ MERGE RIGHTS {TARGET} @{OTHER}: x", author="agent-lead")
        r.mesh.add_comment(card, f"merge gate: ⛔ MERGE RIGHTS {TARGET} @{SHA}: x", author="agent-lead")
        a = self.check(r, a)
        self.assertEqual((a.state, len(self.runs())), ("waiting", 1))
        # positive control: the lead's runner note for this head
        r.mesh.add_comment(card, f"[runner] merge gate: ⛔ MERGE RIGHTS {TARGET} @{SHA}: x", author="agent-lead")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("refused too", a.wake_text)

    def test_the_lead_note_not_written_is_retried_then_the_lead_is_told(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        lr, view = self.lead_runner(r.mesh, gl)
        view.fail_comments = 5
        la = lr.store.create("lead", card, "claude")
        m = {"sha": SHA}
        texts = [lr._merge_to_lead(la, {"target": TARGET}, m, (4, RIGHTS)) for _ in range(3)]
        self.assertEqual(texts[:2], ["", ""])
        self.assertIn("could not be written", texts[2])


if __name__ == "__main__":
    import unittest
    unittest.main()

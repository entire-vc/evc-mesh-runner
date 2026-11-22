"""A merge-only card closed without the merge, and a merger that cannot see the project.

Before: the author's merge went to the lead as a merge-only card; the lead's gate got 404 for the
merge request (its GitLab identity had no access to the project) and the lead closed the card as
told. Every later `wait_for on=merge` of the author on the same head found that same closed card by
its stable key, "handed" the merge to it and woke the author at once: no new attempt by the lead, the
cause never quoted, a wake per re-arm. And the lead's 404 read as a generic gate failure, like a
vanished merge request.
"""
import datetime as dt

from mesh_runner import reviewer_merge
from mesh_runner import runner as runner_mod
from mesh_runner.store import iso
from test_merge_rights_lead import MergeRightsToLead, RIGHTS
from test_merge_wait import MergeBench, PROJECT, SHA, TARGET

NOISE = "Python(99217) MallocStackLogging: can't turn off malloc stack logging because it was not enabled.\n"
# glab's own stderr for an MR it cannot see (read 20.11.2026: `glab mr view 99999999 --repo ...`), cut and
# joined the way the gate's refusal does (`ОТКАЗ: ...: <last three stderr lines joined by " | ">`)
NO_ACCESS = ("ОТКАЗ: GitLab operation failed (exit 1); state not accepted: "
             "ERROR | Failed to get merge request 56: 404 Not Found.\n") + NOISE * 3
MERGER_NO_ACCESS = "merger-no-access"         # the contract string (a literal: the module imports on main too)
CAUSE = "gate: Failed to get merge request 56: 404 Not Found, lead has no access to g/app; closing as told"


class ClosedMergeOnly(MergeRightsToLead):
    def rearm(self, r, a):
        """The author declares `wait_for on=merge` on the same head again: a fresh merge wait."""
        r.store.update(a, state="waiting", wake_text="", wait={
            "kind": "merge", "target": TARGET, "merge": {"sha": SHA, "runs": 0}, "since": iso(), "started": iso()})
        a = self.check(r, a)                    # the author's gate runs again (its rights may be fixed)
        return self.check(r, a)                 # its result

    def closed_by_lead(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        r.mesh.add_comment(card, CAUSE, author="agent-lead")
        r.mesh.set_category(card, "done")
        a = self.check(r, a)
        return r, a, t, gl, card

    def handovers(self, r, t):
        return [c["body"] for c in r.mesh.comment_log.get(t["id"], []) if "The merge goes to" in c["body"]]

    def test_the_author_wakes_with_the_cause_of_the_closed_card(self):
        r, a, t, gl, card = self.closed_by_lead()
        self.assertEqual(a.state, "ready")
        self.assertIn(f"Cause: {CAUSE}", a.wake_text)
        self.assertIn(f"move #{card[:8]} back to todo", a.wake_text)

    def test_a_rearm_on_the_same_head_is_refused_at_once_with_the_cause_not_handed_to_the_closed_card(self):
        r, a, t, gl, card = self.closed_by_lead()
        for n in range(2):                      # the author re-arms, twice
            a = self.rearm(r, a)
            with self.subTest(rearm=n):
                self.assertEqual(a.state, "ready")              # one wake, on the gate's result
                self.assertNotIn("delegate", a.wait["merge"])   # nothing waits on the closed card
                self.assertIn(f"#{card[:8]} (lead) for this head is already done without the merge", a.wake_text)
                self.assertIn(f"Cause: {CAUSE}", a.wake_text)
                self.assertIn("hands nothing on", a.wake_text)
        self.assertEqual(len(self.runs()), 3)   # the author's own gate ran each time
        self.assertEqual(len(self.handovers(r, t)), 1)          # only the first, real handover
        notes = [c["body"] for c in r.mesh.comment_log[t["id"]] if "is not handed on again" in c["body"]]
        self.assertTrue(notes and all(CAUSE in n for n in notes), notes)
        keys = [k for k in r.mesh.comment_keys if k.startswith("merge-only-closed|")]
        self.assertEqual(len(set(keys)), 1)     # one note per closed card and attempt (Mesh replays the key)
        # the lead's closed card is left as it is: no reassignment, no second card
        self.assertEqual((r.cats.category(r.mesh.tasks[card]), len(r.mesh.created)), ("done", 1))

    def test_a_new_last_comment_on_the_closed_card_keeps_the_note_key(self):
        """The key is the note's identity, not a digest of its cause: a later comment on the closed card
        changes the cause, and a key built from it would post the same note again."""
        r, a, t, gl, card = self.closed_by_lead()
        a = self.rearm(r, a)
        r.mesh.add_comment(card, "still no access, see the access card", author="agent-lead")
        a = self.rearm(r, a)
        self.assertIn("Cause: still no access", a.wake_text)          # the wake quotes the fresh cause
        keys = [k for k in r.mesh.comment_keys if k.startswith("merge-only-closed|")]
        self.assertEqual(set(keys), {f"merge-only-closed|{a.id}|{card}"})

    def test_a_reopened_card_takes_the_merge_again(self):
        r, a, t, gl, card = self.closed_by_lead()
        r.mesh.set_category(card, "todo")        # the cause is dealt with; the card is moved back
        a = self.rearm(r, a)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["merge"]["delegate"]["card"], card)
        self.assertEqual((len(r.mesh.created), len(self.handovers(r, t))), (1, 2))

    def test_a_reopened_card_with_an_old_rights_note_does_not_wake_the_author_at_once(self):
        r, a, t, gl, card = self.closed_by_lead()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        r.mesh.add_comment(card, f"[runner] merge gate: ⛔ MERGE RIGHTS {TARGET} @{SHA}: x", author="agent-lead", at=old)
        r.mesh.set_category(card, "todo")
        a = self.rearm(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "waiting")
        # positive control: the lead's note of this attempt still wakes the author
        r.mesh.add_comment(card, f"[runner] merge gate: ⛔ MERGE RIGHTS {TARGET} @{SHA}: x", author="agent-lead")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("refused too", a.wake_text)

    def test_the_cause_is_the_leads_own_words_not_a_runner_note_after_them(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        r.mesh.add_comment(card, CAUSE, author="agent-lead")
        r.mesh.add_comment(card, "[runner] checkout released", author="agent-lead")
        r.mesh.set_category(card, "done")
        a = self.check(r, a)
        self.assertIn(f"Cause: {CAUSE}", a.wake_text)

    def test_the_lead_gate_404_is_merger_no_access_and_the_author_reads_it_when_closed(self):
        r, a, t, gl = self.handed()
        card = r.mesh.created[0]["id"]
        lr, _ = self.lead_runner(r.mesh, gl)
        la = lr.store.create("lead", card, "claude")
        text = lr._merge_result(la, {"target": TARGET}, {"sha": SHA}, {"state": "opened", "sha": SHA}, (2, NO_ACCESS))
        self.assertTrue(text.startswith(MERGER_NO_ACCESS + ":"), text)
        self.assertIn("Do not close this merge-only card", text)
        self.assertIn("access card", text)
        self.assertIn("It is not a missing MR", text)
        note = [c for c in r.mesh.comment_log[card] if "MERGER NO ACCESS" in c["body"]]
        self.assertEqual((len(note), note[0]["author_id"]), (1, "agent-lead"))
        a = self.check(r, a)                    # the note alone wakes nobody: the lead deals with access
        self.assertEqual(a.state, "waiting")
        r.mesh.add_comment(card, "closing", author="agent-lead")
        r.mesh.set_category(card, "done")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"Cause: ({MERGER_NO_ACCESS}) closing", a.wake_text)


class NoAccess(MergeBench):
    def test_a_404_for_the_open_mr_wakes_with_merger_no_access(self):
        r, a, t, gl = self.waiting()
        self.say(NO_ACCESS, rc=2)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"What changed: {MERGER_NO_ACCESS}: the merge gate for MR {TARGET} got 404", a.wake_text)
        self.assertNotIn("Do not close this merge-only card", a.wake_text)   # an ordinary card
        self.assertFalse(any("MERGER NO ACCESS" in c["body"] for c in r.mesh.comment_log.get(t["id"], [])))

    def test_other_failures_stay_generic(self):
        r, a, t, gl = self.waiting()
        mr = {"state": "opened", "sha": SHA}
        positive = r._merge_result(a, {"target": TARGET}, {"sha": SHA}, mr, (2, NO_ACCESS))
        self.assertIn(MERGER_NO_ACCESS, positive)                                 # the control
        for tail in (NO_ACCESS.replace("merge request 56", "merge request 57"),     # another MR
                     NO_ACCESS.replace("merge request 56", "merge request 560"),
                     "ОТКАЗ: токен glab протух (GitLab отвечает 401). Это НЕ отсутствие прав\n",
                     "ОТКАЗ: GitLab operation failed (exit 1); state not accepted: 500 Internal Server Error\n",
                     "ОТКАЗ: Failed to get merge request 56: 403 Forbidden\n"):
            with self.subTest(tail=tail[:60]):
                text = r._merge_result(a, {"target": TARGET}, {"sha": SHA}, mr, (2, tail))
                self.assertIn("failed (exit 2)", text)
                self.assertNotIn(MERGER_NO_ACCESS, text)

    def test_matcher_shapes(self):
        merge_404 = runner_mod.merge_404
        self.assertEqual(runner_mod.MERGER_NO_ACCESS, MERGER_NO_ACCESS)
        self.assertTrue(merge_404("failed to get merge request 56: 404 Not Found", "56"))
        self.assertTrue(merge_404('{"error":{"message":"failed to get merge request 56: 404 Not Found"}}', "56"))
        self.assertTrue(merge_404("GET .../projects/g%2Fapp/merge_requests/56: 404 {message: 404 Not found}", "56"))
        self.assertTrue(merge_404(NOISE + "Failed to get merge request !56: 404\n" + NOISE, "56"))
        self.assertFalse(merge_404("failed to get merge request 56: 403 Forbidden", "56"))
        self.assertFalse(merge_404("failed to get merge request 5: 404 Not Found", "56"))
        self.assertFalse(merge_404("404 Not Found\nmerge request 56", "56"))   # not one line


if __name__ == "__main__":
    import unittest
    unittest.main()

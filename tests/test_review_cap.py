"""Review attempts without a verdict do not stand until UTC midnight.

A card held three real reviews and one attempt that a spent account stopped
before its first round (limit_switch rc=75 three times, then acceptance-unproven). The four counted
against ``daily_start_cap``, the card was held silently ("daily start cap reached") for hours and
nobody was told. Now:

- an attempt that held no round (the infrastructure stopped it) is not counted;
- at the cap the card is handed up (a note on the card, a request card for the lead), not held;
- someone else's comment after the hand-up, or ``rearm-review``, starts the count again (audited);
- two hand-ups in a row without a verdict and the next one goes one level up; a verdict ends the run.
"""
import contextlib
import datetime as dt
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import FakeMesh, task
from mesh_runner import review_cap
from mesh_runner.store import Store
from test_runner import Bench

# a round without a verdict that ended completed (no failure cooldown): a NEXT, then a turn closed unknown
NO_VERDICT = (("readable_outcome", "next"), "prompt_delivery_unknown", "quiet_resolution")
# such an attempt: a spent account three times, then a prompt never proven delivered
INFRA_ONLY = ("limit_switch", "limit_switch", "prompt_unsent_reconciled", "limit_switch",
              "prompt_delivery_unknown", "quiet_resolution")


def attempt(store, agent, tid, state="completed", events=(), turns=1, ended_minutes_ago=0, fed=True):
    # times are kept to the millisecond: an attempt is never in the same one as a reset or hand-up
    time.sleep(0.002)
    a = store.create(agent, tid, "claude", mode="review")
    ended = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ended_minutes_ago)).isoformat()
    store.db.execute("UPDATE attempts SET state=?, turns=?, updated_at=? WHERE id=?", (state, turns, ended, a.id))
    for event in events:
        store.event(a.id, *((event,) if isinstance(event, str) else event))
    if fed:
        store.mark(agent, tid, "review_fed", a.id)
    time.sleep(0.002)
    return a


class ReviewCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name) / "runner.sqlite")
        self.addCleanup(self.s.db.close)

    def test_an_attempt_stopped_by_the_infrastructure_is_not_counted(self):
        attempt(self.s, "lane", "t", events=INFRA_ONLY, turns=2)                  # spent account
        attempt(self.s, "lane", "t", state="failed", turns=3)                     # harness errors only
        attempt(self.s, "lane", "t", state="failed", events=("auth_only_turn", "provider_outage"))
        attempt(self.s, "lane", "t", state="failed", turns=0)                     # spawn never ran a turn
        self.assertEqual(self.s.review_starts_today("lane", "t"), 0)
        # positive control: an ordinary attempt (a round read to its end) counts, verdict or not
        attempt(self.s, "lane", "t", events=NO_VERDICT)
        attempt(self.s, "lane", "t", state="failed", events=("no_outcome",))
        attempt(self.s, "lane", "t", events=(("readable_outcome", "review"),))
        self.assertEqual(self.s.review_starts_today("lane", "t"), 3)
        # another lane, another task, a work attempt: not this count
        attempt(self.s, "other", "t", events=NO_VERDICT)
        attempt(self.s, "lane", "u", events=NO_VERDICT)
        w = self.s.create("lane", "t", "claude")
        self.s.db.execute("UPDATE attempts SET state='completed', turns=1 WHERE id=?", (w.id,))
        self.s.event(w.id, "readable_outcome", "next")
        self.assertEqual(self.s.review_starts_today("lane", "t"), 3)

    def test_a_reset_moves_the_floor_and_midnight_still_bounds_it(self):
        for _ in range(2):
            attempt(self.s, "lane", "t", events=NO_VERDICT)
        self.assertEqual(self.s.review_starts_today("lane", "t", since=dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)), 2)
        rec = review_cap.record_reset(self.s, "lane", "t", by="lead", reason="r", source="comment")
        self.assertEqual(self.s.review_starts_today("lane", "t", since=review_cap.floor(self.s, "lane", "t")), 0)
        attempt(self.s, "lane", "t", events=NO_VERDICT)
        self.assertEqual(self.s.review_starts_today("lane", "t", since=review_cap.floor(self.s, "lane", "t")), 1)
        audit = json.loads(self.s.mark("lane", "t", review_cap.AUDIT + rec["id"])[0])
        self.assertEqual((audit["by"], audit["source"]), ("lead", "comment"))


class ReviewCapHandUp(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.daily_start_cap = 4
        self.agent.lead = "lead"
        self.t = task(1, cat="review")
        self.mesh = FakeMesh([self.t], max_concurrent=1)
        self.r = self.runner(self.mesh)

    def hold(self):
        self.r._hold_cache.clear()
        return self.r.hold_reason(dict(self.mesh.tasks[self.t["id"]], _mode="review"), "review")

    def rounds(self, n, events=NO_VERDICT):
        for _ in range(n):
            attempt(self.r.store, "builder", self.t["id"], events=events)

    def notes(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], []) if "REVIEW CAP" in c["body"]]

    def test_infrastructure_attempts_do_not_reach_the_cap(self):
        # the live case: three real rounds and one attempt a spent account stopped: fed again, nobody bothered
        self.rounds(3)
        attempt(self.r.store, "builder", self.t["id"], events=INFRA_ONLY, turns=2)
        self.assertEqual(self.hold(), "")
        self.assertEqual(self.notes(), [])

    def test_the_cap_hands_up_to_the_lead_and_only_a_comment_reopens_it(self):
        self.rounds(3)
        self.assertEqual(self.hold(), "")
        self.rounds(1)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            reason = self.hold()
        self.assertEqual(reason, "review: 4 review attempts ended without a verdict; handed to lead, "
                                 "waits for a comment on the card or rearm-review")
        self.assertTrue(any("review cap: 4 attempts without a verdict; handed to lead" in l for l in logs.output))
        [note] = self.notes()
        self.assertIn("ran 4 review attempts on this card since UTC midnight", note)
        self.assertIn("Handed to lead (hand-up 1 in a row without a verdict)", note)
        self.assertIn("Reviewer: builder", note)
        self.assertIn("MR: none linked", note)
        for a in self.r.store.review_attempts_since("builder", self.t["id"], review_cap.floor(
                self.r.store, "builder", self.t["id"])):
            self.assertIn(f"`{a.id[:8]}`", note)
        [card] = self.mesh.created
        self.assertEqual(card["assignee_id"], "agent-lead")
        self.assertIn(f"review-cap-request:{self.t['id']}", card["description"])
        self.assertIn("mention-request", card["labels"])
        # no answer yet: held, no second note or card; a runner note or the agent's own word is no answer
        self.mesh.add_comment(self.t["id"], "[runner] some note", author="agent-reviewer")
        self.mesh.add_comment(self.t["id"], "still looking", author=self.r.agent_id)
        self.assertEqual(self.hold(), reason)
        self.assertEqual((len(self.notes()), len(self.mesh.created)), (1, 1))
        # the lead's word starts the count again: fed now, and the reset names its author
        self.mesh.add_comment(self.t["id"], "MR head fixed, review it again", author="agent-lead")
        self.assertEqual(self.hold(), "")
        rec = review_cap.reset(self.r.store, "builder", self.t["id"])
        self.assertEqual((rec["by"], rec["source"]), ("agent-lead", "comment"))
        self.assertIn("after the hand-up to lead", rec["reason"])
        self.rounds(3)
        self.assertEqual(self.hold(), "")              # the guard stays: the cap again, from the reset
        self.rounds(1)
        self.assertIn("handed to lead", self.hold())
        self.assertEqual(len(self.notes()), 2)

    def test_rearm_review_reopens_a_handed_up_review(self):
        self.rounds(4)
        self.assertIn("handed to lead", self.hold())
        review_cap.request(self.r.store, "builder", self.t["id"], "account restored", by="operator:test", apply=True)
        self.assertEqual(self.hold(), "")
        self.assertEqual(review_cap.reset(self.r.store, "builder", self.t["id"])["source"], "rearm-review")

    def test_two_hand_ups_in_a_row_go_one_level_up_and_a_verdict_ends_the_run(self):
        self.cfg.project_leads = {"p1": "boss"}
        self.mesh.agents.append({"id": "agent-boss", "name": "boss"})
        for n in range(3):
            self.rounds(4)
            self.assertIn("handed to " + ("lead" if n < 2 else "boss"), self.hold())
            self.mesh.add_comment(self.t["id"], f"try again {n}", author="agent-lead")
            self.assertEqual(self.hold(), "")
        self.assertIn("Handed to boss (hand-up 3 in a row without a verdict, past lead)", self.notes()[-1])
        self.assertEqual([c["assignee_id"] for c in self.mesh.created], ["agent-lead", "agent-lead", "agent-boss"])
        # a verdict: the run is over, the next hand-up goes to the nearest lead again
        attempt(self.r.store, "builder", self.t["id"], events=(("readable_outcome", "review"),), ended_minutes_ago=1)
        self.hold()
        self.assertIsNone(review_cap.handup(self.r.store, "builder", self.t["id"]))
        self.rounds(3)
        self.assertIn("handed to lead", self.hold())
        self.assertIn("hand-up 1 in a row", self.notes()[-1])

    def test_without_a_lead_it_says_so_and_holds(self):
        self.agent.lead = ""
        self.rounds(4)
        self.assertIn("handed to nobody", self.hold())
        [note] = self.notes()
        self.assertIn("No lead to hand it to", note)
        self.assertEqual(self.mesh.created, [])

    def test_a_failed_request_card_is_filed_on_a_later_pass(self):
        self.mesh.fail_create = 1
        self.rounds(4)
        self.assertIn("handed to lead", self.hold())
        self.assertEqual(self.mesh.created, [])
        self.assertIn("handed to lead", self.hold())
        self.assertEqual([c["assignee_id"] for c in self.mesh.created], ["agent-lead"])
        self.assertTrue(review_cap.handup(self.r.store, "builder", self.t["id"])["request"])
        self.hold()
        self.assertEqual(len(self.mesh.created), 1)
        self.assertEqual(len(self.notes()), 1)


class RearmReviewCli(unittest.TestCase):
    TID = "00000000-0000-0000-0000-000000000001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runner.sqlite"
        self.s = Store(self.path)
        self.addCleanup(self.s.db.close)
        self.cfg = SimpleNamespace(state_dir=self.path.parent, agents=[SimpleNamespace(name="lane")])

    def cli(self, *extra):
        from mesh_runner.cli import main
        args = ["rearm-review", "--config", "unused", "--agent", "lane", "--task", self.TID, *extra]
        with mock.patch("mesh_runner.cli.config_mod.load", return_value=self.cfg), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = main(args)
        return rc, json.loads(out.getvalue())

    def test_dry_run_writes_nothing_and_apply_resets_with_an_audit_record(self):
        for _ in range(4):
            attempt(self.s, "lane", self.TID, events=NO_VERDICT)
        rc, out = self.cli("--reason", "account restored", "--dry-run")
        self.assertEqual((rc, out["eligible"], out["applied"], out["counted_attempts"]), (0, True, False, 4))
        self.assertIsNone(review_cap.reset(self.s, "lane", self.TID))
        rc, out = self.cli("--reason", "account restored", "--apply")
        self.assertEqual((rc, out["applied"]), (0, True))
        rec = review_cap.reset(self.s, "lane", self.TID)
        self.assertEqual(rec["id"], out["reset"])
        self.assertTrue(rec["by"].startswith("operator:"))
        self.assertEqual((rec["reason"], rec["source"]), ("account restored", "rearm-review"))
        self.assertIsNotNone(self.s.mark("lane", self.TID, review_cap.AUDIT + rec["id"]))
        self.assertEqual(self.s.review_starts_today("lane", self.TID, since=review_cap.floor(self.s, "lane", self.TID)), 0)
        # nothing left to reset: refused, and so is a task with an open attempt
        rc, out = self.cli("--reason", "again", "--apply")
        self.assertEqual((rc, out["eligible"]), (1, False))
        self.assertIn("no review attempts to reset", out["reason"])
        attempt(self.s, "lane", self.TID, events=NO_VERDICT)
        self.s.create("lane", self.TID, "claude", mode="review")             # starting: open
        rc, out = self.cli("--reason", "again", "--apply")
        self.assertEqual((rc, out["reason"]), (1, "task has an open attempt of this agent"))

    def test_reason_and_mode_are_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.cli("--apply")
            with self.assertRaises(SystemExit):
                self.cli("--reason", "x", "--apply", "--dry-run")


if __name__ == "__main__":
    unittest.main()

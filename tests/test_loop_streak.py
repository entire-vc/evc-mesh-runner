"""Loops are caught by repetition without progress (loop_streak_limit), not by a daily count."""
import datetime as dt
import json
import unittest

from fakes import AGENT_ID, FakeMesh, calls, task, write_replies, FUTURE_AT
from test_requeue_on_cap import CapBench
from test_runner import Bench

from mesh_runner.outcome import Outcome
from mesh_runner.runner import LOOP_STREAK


def now():
    return dt.datetime.now(dt.timezone.utc)


FINDINGS = [
    "the flaky test comes from the shared fixture, not from the change under review",
    "the migration needs an index on the owner column before the backfill can run",
    "the retry helper swallows the timeout, so the caller never sees the failure",
    "the staging queue is drained and the worker restarted cleanly after the deploy",
    "the config schema rejects an empty list, so the default has to move to the loader",
    "the cache key ignores the locale, which explains the stale German copy on the page",
    "the webhook signature check compares bytes after decoding, which breaks on a trailing newline",
    "the export job holds the connection open while it waits for the object store",
    "the dashboard query scans the whole events table because the date filter is cast to text",
    "the release notes list the flag but the flag is still off in the production config",
    "the lint rule is correct, the offending import was added by the generated client",
    "the rollback script restores the schema but not the sequence, so ids collide afterwards",
]


class LoopBench(Bench):
    """The fleet runs with the daily counters off: only the streak guards against loops."""

    def setUp(self):
        super().setUp()
        self.cfg.daily_next_budget = 100000
        self.cfg.daily_wait_budget = 100000
        self.cfg.daily_step_budget = 100000
        self.cfg.max_turns = 60
        self.agent.lead = "lead"
        self.digest = ["same"]

    def start(self, reply, mesh=None, digest=True):
        self.t = task(1)
        self.mesh = mesh or FakeMesh([self.t], max_concurrent=1)
        write_replies(self.replies, {self.t["id"][:8]: reply})
        r = self.runner(self.mesh)
        if digest:
            r._work_digest = lambda a: self.digest[0]
        return r

    def bodies(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])]

    def looped(self):
        return [b for b in self.bodies() if "LOOP" in b]

    def turns(self):
        return len(calls(self.calls))

    def say(self, text):
        """The agent writes on the card during the running turn."""
        self.mesh.add_comment(self.t["id"], text, author=AGENT_ID, at=now())

    def next_turns(self, r, n, per_turn=None):
        """Drive n turns of a NEXT loop one at a time; stop early when the task leaves the agent."""
        r.tick()
        for i in range(n):
            if per_turn:
                per_turn(i)
            if self.finish_turn(r).state != "ready":
                return i + 1
            r.tick()
        return n

    def step(self, r, reply, recheck=False, during=None):
        """One turn, driven by hand: wakes a waiting session with a comment from someone else (as a
        runner re-check when ``recheck``), or runs the ready one. Returns the settled attempt."""
        write_replies(self.replies, {self.t["id"][:8]: reply})
        waiting = r.store.by_state(self.agent.name, ("waiting",))
        if waiting:
            if recheck:
                r.store.mark(self.agent.name, self.t["id"], "recheck_wake", f"{waiting[0].id}:1")
            self.mesh.add_comment(self.t["id"], f"ping {self.turns()}", author="someone")
        r.tick()
        if during:
            during()                                   # work done while the turn runs
        return self.finish_turn(r)

    def wake(self, r, reply, before=None):
        """One wait round: a comment from someone else wakes the waiting session, which runs a turn."""
        write_replies(self.replies, {self.t["id"][:8]: reply})
        if before:
            before(r.store.by_state(self.agent.name, ("waiting",))[0])
        self.mesh.add_comment(self.t["id"], f"ping {self.turns()}", author="someone")
        self.settle_all(r)


class RunsOfNothing(LoopBench):
    def test_a_run_of_empty_nexts_is_handed_up_at_the_limit(self):
        r = self.start("↻ NEXT")
        self.settle_all(r)
        self.assertEqual(self.turns(), 6)
        banner = self.looped()
        self.assertEqual(len(banner), 1)
        self.assertIn("6 turns in a row without progress", banner[0])
        self.assertIn("NEXT", banner[0])
        self.assertEqual(self.mesh.tasks[self.t["id"]]["assignee_id"], "agent-lead")

    def test_six_bounced_identical_waits_are_handed_up(self):
        # the runner sends the wait back each time (here the card-wait bounce, which has no
        # once-a-day limit) and the agent declares it again
        r = self.start("⏳ WAIT card:#00000002")
        r._card_wait_told = lambda a, outcome: "that card waits on a timer: wait on the event"
        self.settle_all(r)
        self.assertEqual(self.turns(), 7)                 # the first, then six sent back and declared again
        banner = self.looped()
        self.assertEqual(len(banner), 1)
        self.assertIn("sent back by the runner and declared again", banner[0])
        self.assertEqual(self.mesh.tasks[self.t["id"]]["assignee_id"], "agent-lead")

    def test_a_wait_declared_again_in_a_turn_started_by_its_own_next_extends_it(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor")
        self.settle_all(r)
        for _ in range(3):
            self.assertEqual(self.looped(), [])
            self.assertEqual(self.step(r, "↻ NEXT").state, "ready")
            self.step(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor")
        self.assertEqual(self.turns(), 7)
        self.assertIn("started by its own NEXT", self.looped()[0])

    def test_the_banner_lists_the_turns(self):
        r = self.start("↻ NEXT")
        self.settle_all(r)
        text = self.looped()[0]
        for n in range(1, 7):
            self.assertIn(f"{n}. NEXT, nothing changed, no new comment", text)

    def test_the_streak_starts_again_after_a_hand_up(self):
        r = self.start("↻ NEXT")
        self.settle_all(r)
        self.assertEqual(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0], "")


class AcceptedWaitsAreNeutral(LoopBench):
    def test_ten_rechecks_of_an_accepted_decision_wait_never_hand_up(self):
        r = self.start("⏳ WAIT decision:@lead: ship the release?")
        self.settle_all(r)
        self.assertEqual(self.states(r), {self.t["id"][:8]: "waiting"})
        for _ in range(10):
            self.assertEqual(self.step(r, "⏳ WAIT decision:@lead: ship the release?", recheck=True).state, "waiting")
        self.assertEqual(self.turns(), 11)
        self.assertEqual(self.looped(), [])

    def test_the_same_accepted_wait_after_any_wake_is_neutral(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor")
        self.settle_all(r)
        for _ in range(12):
            self.wake(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor")
        self.assertEqual(self.turns(), 13)
        self.assertEqual(self.looped(), [])

    def test_a_neutral_recheck_between_empty_nexts_does_not_reset_the_count(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor")
        self.settle_all(r)
        self.assertEqual(self.step(r, "↻ NEXT").state, "ready")                                  # 1
        self.assertEqual(self.step(r, "↻ NEXT").state, "ready")                                  # 2
        self.assertEqual(self.step(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor").state, "waiting")    # 3: its own NEXT
        self.assertEqual(self.step(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor", recheck=True).state, "waiting")
        for _ in range(2):                                                                       # 4, 5
            self.assertEqual(self.step(r, "↻ NEXT").state, "ready")
        self.assertEqual(self.looped(), [])
        self.assertEqual(self.step(r, "↻ NEXT").state, "completed")                              # 6: handed up
        self.assertEqual(len(self.looped()), 1)

    def test_a_recheck_that_ends_in_a_different_wait_is_progress(self):
        r = self.start("↻ NEXT")
        r.tick()
        for _ in range(4):
            self.finish_turn(r)
            r.tick()
        self.assertEqual(self.step(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 waiting for the vendor").state, "waiting")    # a different wait: reset
        self.assertEqual(json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])["log"], [])


class ProgressIsNeverStopped(LoopBench):
    def test_nexts_that_change_the_worktree_are_never_a_loop(self):
        r = self.start("↻ NEXT")
        counter = iter(range(1000))
        r._work_digest = lambda a: str(next(counter))
        self.settle_all(r)                    # only max_turns ends it
        self.assertGreaterEqual(self.turns(), 50)
        self.assertEqual(self.looped(), [])

    def test_many_waits_on_different_things_are_never_a_loop(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak round 0")
        self.settle_all(r)
        for i in range(1, 16):
            self.wake(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak round {i}")
        self.assertEqual(self.turns(), 16)
        self.assertEqual(self.looped(), [])
        self.assertEqual(self.states(r), {self.t["id"][:8]: "waiting"})

    def test_the_same_wait_after_a_met_event_is_a_step_not_a_loop(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 the release train")
        self.settle_all(r)
        for _ in range(15):
            # the awaited thing happened and woke the session; it waits for the next round of the same
            self.wake(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 the release train",
                      lambda a: r.store.mark(self.agent.name, self.t["id"], "met_wake", a.id))
        self.assertEqual(self.turns(), 16)
        self.assertEqual(self.looped(), [])

    def test_the_same_wait_with_a_new_comment_each_time_is_not_a_loop(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 review of the change")
        self.settle_all(r)
        write_replies(self.replies, {self.t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 review of the change"})
        for i in range(12):
            self.mesh.add_comment(self.t["id"], f"ping {i}", author="someone")
            r.tick()                                   # the comment wakes the session and it starts a turn
            self.say(f"{FINDINGS[i]}")                 # written while the turn runs
            self.assertEqual(self.finish_turn(r).state, "waiting")
        self.assertEqual(self.turns(), 13)
        self.assertEqual(self.looped(), [])


class ResetsTheStreak(LoopBench):
    def test_an_own_comment_resets_it(self):
        r = self.start("↻ NEXT")
        r.tick()
        for turn in range(1, 12):
            if turn == 6:
                self.say("found the cause: the cache key ignores the locale, trying a fix")
            self.assertEqual(self.finish_turn(r).state, "ready", f"turn {turn}")
            r.tick()
        self.assertEqual(self.looped(), [])           # 5 empty, comment, 5 empty: the streak is 5
        self.assertEqual(self.finish_turn(r).state, "completed")   # the 12th empty turn: 6 in a row
        self.assertEqual(len(self.looped()), 1)

    def test_the_same_comment_again_does_not_reset_it(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 12, lambda i: self.say("still looking at the failing job, nothing found yet"))
        self.assertEqual(n, 7)                        # the first copy is new, then six repeats
        self.assertEqual(len(self.looped()), 1)

    def test_a_declaration_alone_is_not_a_comment(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 12, lambda i: self.say("↻ NEXT: " + str(i)))
        self.assertEqual(n, 6)
        self.assertEqual(len(self.looped()), 1)

    def test_a_runner_note_is_not_a_comment(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 12, lambda i: self.say(f"[runner] note {i}"))
        self.assertEqual(n, 6)

    def test_a_change_in_the_worktree_resets_it(self):
        r = self.start("↻ NEXT")
        r.tick()
        for turn in range(1, 12):
            if turn == 6:
                self.digest[0] = "work done in turn 6"
            self.assertEqual(self.finish_turn(r).state, "ready", f"turn {turn}")
            r.tick()
        self.assertEqual(self.looped(), [])
        self.assertEqual(self.finish_turn(r).state, "completed")
        self.assertEqual(len(self.looped()), 1)

    def test_an_unreadable_digest_is_not_proof_of_idleness(self):
        r = self.start("↻ NEXT")
        r._work_digest = lambda a: ""
        self.settle_all(r)
        self.assertEqual(self.looped(), [])

    def test_a_task_without_a_worktree_relies_on_comments(self):
        r = self.start("↻ NEXT")
        r.tick()
        a = r.store.by_state(self.agent.name, ("running",))[0]
        r.store.update(a, workspaces=[])
        self.digest[0] = "ignored"
        n = 1
        while self.finish_turn(r).state == "ready":
            n += 1
            r.tick()
        self.assertEqual(n, 6)

    def test_a_changed_status_and_a_met_wake_reset_it_directly(self):
        r = self.start("↻ NEXT")
        r.refresh_me()
        a = r.store.create(self.agent.name, self.t["id"], "claude")
        r.store.update(a, turn_started_at=(now() - dt.timedelta(minutes=1)).isoformat())
        full = {"log": ["NEXT, nothing changed, no new comment"] * 5, "sig": "", "seen": [], "cmts": [], "st": "S1", "met": ""}
        mark = lambda: r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK, json.dumps(full))
        mark()
        card = lambda status: dict(self.mesh.tasks[self.t["id"]], status_id=status)
        self.assertFalse(r._loop_guard(a, card("S2"), Outcome("next"), [], ""))
        self.assertEqual(json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])["log"], [])
        mark()
        self.assertTrue(r._loop_guard(a, card("S1"), Outcome("next"), [], ""))    # unchanged: the sixth
        b = r.store.create(self.agent.name, "other-task", "claude")
        r.store.update(b, turn_started_at=(now() - dt.timedelta(minutes=1)).isoformat())
        r.store.mark(self.agent.name, "other-task", LOOP_STREAK, json.dumps(full))
        r.store.mark(self.agent.name, "other-task", "met_wake", b.id)
        self.assertFalse(r._loop_guard(b, card("S1"), Outcome("next"), [], ""))


class NothingNew(LoopBench):
    def test_comments_that_differ_only_in_numbers_and_times_are_not_news(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 12, lambda i: self.say(
            f"still waiting for the release pipeline to finish (pipeline {21234 + i}, checked 14:{i:02d}, "
            f"sha {i}a3f9c{i:02d}e on 2026-10-{10 + i})"))
        self.assertEqual(n, 7)                        # the first copy is news, then six look the same
        self.assertEqual(len(self.looped()), 1)

    def test_a_status_line_too_short_to_carry_news_never_counts(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 12, lambda i: self.say(f"still waiting for pipeline {21234 + i} (checked 14:0{i})"))
        self.assertEqual(n, 6)
        self.assertEqual(len(self.looped()), 1)

    def test_a_genuinely_new_finding_resets_it(self):
        r = self.start("↻ NEXT")
        r.tick()
        said = lambda i: f"still waiting for the release pipeline to finish (pipeline {21234 + i}, checked 14:0{i})"
        for turn in range(1, 12):
            self.say(FINDINGS[0] if turn == 7 else said(turn))
            self.assertEqual(self.finish_turn(r).state, "ready", f"turn {turn}")   # 1 news, 5 repeats, news, 4 repeats
            if turn < 11:
                r.tick()
        self.assertEqual(self.looped(), [])

    def test_alternating_between_two_comments_is_still_a_repeat(self):
        r = self.start("↻ NEXT")
        n = self.next_turns(r, 14, lambda i: self.say(FINDINGS[i % 2]))
        self.assertEqual(n, 8)                        # two news, then six repeats
        self.assertEqual(len(self.looped()), 1)

    def test_the_gist_drops_numbers_times_dates_ids_and_query_strings(self):
        from mesh_runner.runner import AgentRunner
        gist = AgentRunner._comment_gist
        a = gist("Still waiting for pipeline 21234 (checked 14:05) - see https://git.example/x/-/pipelines/21234?page=2&scope=all, "
                 "commit 4739602a065f on 2026-10-28T14:05:11Z")
        b = gist("STILL waiting for pipeline 99 (checked 9:41 pm)... see https://git.example/x/-/pipelines/99?foo=bar, "
                 "commit deadbeef0 on 2027-01-02 07:30")
        self.assertEqual(a, b)
        self.assertNotIn("14", a)
        self.assertNotEqual(a, gist("Pipeline failed in the lint job: the generated client import is wrong"))


class PingPong(LoopBench):
    A, B = f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak alpha", f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak beta"

    def test_alternating_between_two_waits_is_handed_up(self):
        r = self.start(self.A)
        self.settle_all(r)
        for i, reply in enumerate([self.B, self.A, self.B, self.A, self.B, self.A]):
            self.assertEqual(self.step(r, reply).state, "waiting", f"turn {i + 2}")
            self.assertEqual(self.looped(), [])
        self.assertEqual(self.step(r, self.B).state, "completed")        # A, B new; six repeats
        self.assertEqual(self.turns(), 8)
        banner = self.looped()
        self.assertEqual(len(banner), 1)
        self.assertIn("alternating", banner[0])

    def test_progress_clears_the_window_completely(self):
        # A, progress (the digest moves) with B, then A: A was not declared in the current window
        r = self.start(self.A)
        self.settle_all(r)
        self.assertEqual(self.step(r, self.B, during=lambda: self.digest.__setitem__(0, "work done")).state, "waiting")
        self.assertEqual(self.step(r, self.A).state, "waiting")
        state = json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])
        self.assertEqual(state["log"], [])
        self.assertEqual(len(state["seen"]), 2)           # B, then A: both declared after the progress
        for reply in (self.B, self.A, self.B):             # now they really alternate
            self.assertEqual(self.step(r, reply).state, "waiting")
        self.assertEqual(len(json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])["log"]), 3)
        self.assertEqual(self.looped(), [])

    def test_progress_by_a_next_clears_the_window_too(self):
        r = self.start(self.A)
        self.settle_all(r)
        self.assertEqual(self.step(r, "↻ NEXT", during=lambda: self.digest.__setitem__(0, "work done")).state,
                         "ready")                                          # progress
        self.assertEqual(self.step(r, self.B).state, "waiting")
        self.assertEqual(self.step(r, self.A).state, "waiting")
        state = json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])
        self.assertEqual(state["log"], [])

    def test_all_new_waits_are_never_a_loop(self):
        r = self.start(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak a")
        self.settle_all(r)
        for name in "bcdefghijkl":
            self.assertEqual(self.step(r, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak {name}").state, "waiting")
        self.assertEqual(self.turns(), 12)
        self.assertEqual(self.looped(), [])

    def test_a_met_event_starts_a_new_window(self):
        r = self.start(self.A)
        self.settle_all(r)
        replies = [self.B, self.A, self.B, self.A, self.B, self.A, self.B, self.A]
        for reply in replies:
            r.store.mark(self.agent.name, self.t["id"], "met_wake", r.store.by_state(self.agent.name, ("waiting",))[0].id)
            self.assertEqual(self.step(r, reply).state, "waiting")
        self.assertEqual(self.looped(), [])

    def test_the_window_is_forgotten_after_a_status_change_directly(self):
        r = self.start("↻ NEXT")
        r.refresh_me()
        a = r.store.create(self.agent.name, self.t["id"], "claude")
        r.store.update(a, turn_started_at=(now() - dt.timedelta(minutes=1)).isoformat())
        alpha, beta = (r._loop_signature(Outcome("time", note=n)) for n in ("soak alpha", "soak beta"))
        state = {"log": ["x"] * 3, "sig": beta, "seen": [alpha, beta], "cmts": [], "st": "S1", "met": "", "last": "wait"}
        r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK, json.dumps(state))
        card = dict(self.mesh.tasks[self.t["id"]], status_id="S2")
        self.assertFalse(r._loop_guard(a, card, Outcome("time", note="soak alpha"), [], ""))
        got = json.loads(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK)[0])
        self.assertEqual((got["log"], got["seen"]), ([], [alpha]))             # the old window is gone; only this turn's wait is in it


class Off(LoopBench):
    def test_limit_zero_disables_the_guard(self):
        self.cfg.loop_streak_limit = 0
        self.cfg.max_turns = 20
        r = self.start("↻ NEXT")
        self.settle_all(r)
        self.assertEqual(self.turns(), 20)
        self.assertEqual(self.looped(), [])
        self.assertIsNone(r.store.mark(self.agent.name, self.t["id"], LOOP_STREAK))

    def test_the_daily_counters_still_work_as_a_backstop(self):
        self.cfg.loop_streak_limit = 0
        self.cfg.daily_next_budget = 2
        r = self.start("↻ NEXT")
        self.settle_all(r)
        self.assertTrue(any("TURN CAP" in b for b in self.bodies()))


class Routing(CapBench):
    def setUp(self):
        super().setUp()
        self.cfg.daily_next_budget = 100000
        self.cfg.max_turns = 60
        self.t = task(1)
        self.mesh = FakeMesh([self.t], max_concurrent=1)
        write_replies(self.replies, {self.t["id"][:8]: "↻ NEXT"})

    def test_a_loop_follows_requeue_on_cap(self):
        self.agent.lead = "lead"
        r = self.runner(self.mesh)
        r._work_digest = lambda a: "same"
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 6)
        self.assertIn("cap: requeued (loop)", self.notes(r, self.t))
        self.assertEqual(self.mesh.tasks[self.t["id"]]["assignee_id"], AGENT_ID)     # no lead hand-off
        self.assertTrue(any("LOOP" in b for b in self.bodies(self.mesh, self.t)))
        self.assertTrue(r._cap_wait(self.t["id"]))
        self.assertNotIn(self.t["id"], r._task_backoff)

    def test_a_loop_without_a_lead_waits_for_a_comment_not_for_tomorrow(self):
        self.cfg.requeue_on_cap = False
        r = self.runner(self.mesh)
        r._work_digest = lambda a: "same"
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 6)
        body = [b for b in self.bodies(self.mesh, self.t) if "LOOP" in b][0]
        self.assertIn("until someone writes here", body)
        self.assertNotIn("tomorrow", body)
        self.assertNotIn(self.t["id"], r._task_backoff)

    def test_a_lead_that_cannot_take_it_does_not_hold_it_until_tomorrow(self):
        self.cfg.requeue_on_cap = False
        self.agent.lead = "lead"
        r = self.runner(self.mesh)
        r._work_digest = lambda a: "same"
        r.store.mark(self.agent.name, self.t["id"], "handed_up", dt.datetime.now(dt.timezone.utc).date().isoformat())
        self.settle_all(r)
        body = [b for b in self.bodies(self.mesh, self.t) if "LOOP" in b][0]
        self.assertIn("came back", body)
        self.assertNotIn("tomorrow", body)
        self.assertNotIn(self.t["id"], r._task_backoff)


class ConfigLimit(unittest.TestCase):
    def test_default_and_validation(self):
        from mesh_runner.config import ConfigError, RunnerConfig, _loop_streak_limit
        self.assertEqual(RunnerConfig(mesh_url="http://mesh", state_dir="/tmp/x", agents=[]).loop_streak_limit, 6)
        self.assertEqual(_loop_streak_limit(0), 0)
        for bad in (-1, 2.5, "6", True):
            with self.assertRaises(ConfigError):
                _loop_streak_limit(bad)


if __name__ == "__main__":
    unittest.main()

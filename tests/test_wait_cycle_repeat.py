"""A wait cycle is told once per signature, and a question is not converted into one.

Observed shape: agent A asked ``⏳ WAIT decision:@b: ... #<B's card>``; the runner converted it into
a card wait on B's live card, which itself waited ``decision:@a``. The cycle notice told A to ask
with ``decision:@b`` instead, which was converted again: a cycle wake and a resumed turn every two
minutes, dozens of times, until the turn cap handed the card up. Two rules end it:

1. a decision about the addressee's live task that waits on the asker is filed as a request, not
   converted into a card wait (the cycle);
2. the cycle notice is durable per card and signature (store mark ``cycle_told``): the same wait
   declared again, by any session, after a restart or a replay, is not woken again; a changed
   question on the other side wakes once. The mark is task-wide, so a card reassigned to another
   agent is not told the same cycle again.

Names are spelled out instead of imported so this file runs against the old code as a red control.
"""
import json

from fakes import FakeMesh, calls, task, write_replies
from mesh_runner.config import AgentConfig
from mesh_runner.runner import TASK_WIDE, AgentRunner
from mesh_runner.store import Store
from test_runner import Bench

OTHER8 = "abcdef12"
MINE8 = "00000001"                   # task(1): the agent's own card


def events(r, attempt_id):
    return [kind for _, kind, _ in r.store.events(attempt_id)]


def all_events(r):
    out = []
    for (aid,) in r.store.db.execute("SELECT id FROM attempts"):
        out += events(r, aid)
    return out


class MutualCardCycle(Bench):
    """Each card waits for the other to close: the agent's card wait is accepted (the target waits
    for code-like progress, a card), so only the cycle check can wake it."""

    def start(self, other_wait):
        self.t, self.other = task(1), task(0xabcdef12, assignee_id="someone-else")
        self.mesh = FakeMesh([self.t, self.other], max_concurrent=1)
        write_replies(self.replies, {self.t["id"][:8]: f"⏳ WAIT card:#{OTHER8}",
                                     self.t["id"][:8] + ":resume": f"⏳ WAIT card:#{OTHER8}"})
        self.r = self.runner(self.mesh)
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.theirs = self.r.store.create("Executor", self.other["id"], "claude", "fixture")
        self.r.store.update(self.theirs, state="waiting", wait=other_wait)

    def ticks(self, n, r=None):
        r = r or self.r
        for _ in range(n):
            r._last_wake_check.clear()
            r.tick()
            self.settle_all(r)

    def cycle_prompts(self):
        return [c["prompt"] for c in calls(self.calls) if "wait cycle" in c["prompt"]]

    def mine(self, r=None):
        r = r or self.r
        return [a for a in r.store.by_state(self.agent.name, ("waiting", "ready", "running"))
                if a.task_id == self.t["id"]]

    def test_the_same_cycle_is_told_once_through_repeated_declarations(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        self.ticks(1)
        self.assertEqual(len(self.cycle_prompts()), 1)             # the one notice
        self.ticks(4)                                              # the same wait declared again, rechecked
        self.assertEqual(len(self.cycle_prompts()), 1)
        self.assertEqual(len(calls(self.calls)), 2)                # no further model turn
        self.assertIn("cycle_wake_suppressed", all_events(self.r))
        waiting = self.mine()
        self.assertEqual([a.state for a in waiting], ["waiting"])  # the declared wait stands
        self.assertEqual(waiting[0].wait["kind"], "card")

    def test_a_restart_or_a_replayed_wait_is_not_told_again(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        self.ticks(2)
        self.assertEqual(len(self.cycle_prompts()), 1)
        for _ in range(3):                                        # restart: a new runner on the same store
            r2 = self.runner(self.mesh)
            for a in self.mine(r2):                               # replay: the wait as first declared
                w = dict(a.wait)
                w.pop("cycle_told", None)
                r2.store.update(a, wait=w)
            self.ticks(2, r2)
        self.assertEqual(len(self.cycle_prompts()), 1)
        self.assertEqual(len(calls(self.calls)), 2)

    def test_a_changed_question_on_the_other_side_wakes_once(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        self.ticks(2)
        self.assertEqual(len(self.cycle_prompts()), 1)
        self.r.store.update(self.r.store.get(self.theirs.id), wait={
            "kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8} after the new migration"})
        self.ticks(3)
        prompts = self.cycle_prompts()
        self.assertEqual(len(prompts), 2)                          # one new notice, then quiet again
        self.assertIn("after the new migration", prompts[-1])

    def test_returning_to_an_earlier_question_is_not_told_again(self):
        first = {"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"}
        second = {**first, "line": f"⏳ WAIT card:#{MINE8} after the new migration"}
        self.start(first)
        self.ticks(2)
        self.r.store.update(self.r.store.get(self.theirs.id), wait=second)
        self.ticks(2)
        self.assertEqual(len(self.cycle_prompts()), 2)            # A told, B told
        self.r.store.update(self.r.store.get(self.theirs.id), wait=first)
        self.ticks(3)
        self.assertEqual(len(self.cycle_prompts()), 2)            # A again: already told
        self.r.store.update(self.r.store.get(self.theirs.id), wait=second)
        self.ticks(3)
        self.assertEqual(len(self.cycle_prompts()), 2)            # B again: already told

    def test_each_field_of_the_other_side_wait_is_part_of_the_signature(self):
        """Checked on the wake check of one waiting attempt: a decision wait on the other side also
        sends a fresh card wait back on declaration (an older rule), which would hide the count."""
        card = {"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"}
        ask = {"kind": "decision", "handle": "builder", "target": "r1", "line": "⏳ WAIT decision:@builder: ok?"}
        variants = [("kind", ask),
                    ("handle", {**ask, "handle": "build-bot"}),
                    ("target", {**ask, "handle": "build-bot", "target": "r2"}),
                    ("line", {**ask, "handle": "build-bot", "target": "r2", "line": "⏳ WAIT decision:@builder: go?"})]
        self.start(card)
        me = self.mesh.me()
        self.mesh.me = lambda: {**me, "slug": "build-bot"}                     # one agent, two handles
        self.r.refresh_me()
        mine = self.mine()[0]

        def told(theirs):
            """The other side asks ``theirs``; this card's wait is declared afresh and checked."""
            self.r.store.update(self.r.store.get(self.theirs.id), wait=theirs)
            a = self.r.store.get(mine.id)
            declared = {k: v for k, v in a.wait.items() if k != "cycle_told"}  # replayed as declared
            self.r.store.update(a, state="waiting", wait=declared, wake_text="")
            self.r._wake_one(self.r.store.get(mine.id))
            a = self.r.store.get(mine.id)
            return a.wake_text if a.state == "ready" else ""

        self.assertIn("wait cycle", told(card))
        for field, theirs in variants:
            with self.subTest(field):
                self.assertIn("wait cycle", told(theirs))                      # a new signature wakes once
                self.assertEqual(told(theirs), "")
        same = {**variants[-1][1], "handle": "BUILD-BOT", "line": "⏳  WAIT decision:@builder:   go?"}
        self.assertEqual(told(same), "")                                       # case and spacing only
        for _, theirs in [("card", card), *variants]:
            self.assertEqual(told(theirs), "")                                 # every one stays told

    def test_a_card_reassigned_to_another_agent_is_not_told_the_same_cycle_again(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        self.ticks(2)
        self.assertEqual(len(self.cycle_prompts()), 1)
        relief = AgentConfig(name="relief", key="k", harness="claude", profile_dir=self.root, repos=[])
        self.cfg.agents.append(relief)
        r2 = AgentRunner(self.cfg, relief, Store(self.root / "state" / "r.sqlite"), mesh=self.mesh,
                         adapter=self.adapter)
        self.addCleanup(r2.store.db.close)
        mine = self.mine()[0]
        a = r2.store.create("relief", self.t["id"], "claude", "fixture")
        wait = {"kind": "card", "target": OTHER8, "line": f"⏳ WAIT card:#{OTHER8}"}
        r2.store.update(a, state="waiting", wait=wait)
        self.assertEqual(r2._wait_cycle(r2.store.get(a.id), dict(wait)), "")
        self.assertIn("cycle_wake_suppressed", events(r2, a.id))
        self.assertNotEqual(mine.agent, "relief")

    def race(self, after_notice):
        """Run ``after_notice`` between the cycle check and the wake's commit, as a concurrent writer."""
        real, on_end = self.r._wait_cycle, self.r.effects.on_end

        def check(a, wait):
            text = real(a, wait)
            if text:
                after_notice(a, wait)
            return text
        self.r._wait_cycle = check
        # the wait's effects (its blocking link) are ended before the commit for every wake kind;
        # kept out of this race so the next check reads the cycle, not a removed link
        self.r.effects.on_end = lambda *args, **kw: None

        def restore():
            self.r._wait_cycle, self.r.effects.on_end = real, on_end
        return restore

    def told_marks(self):
        got = self.r.store.mark(TASK_WIDE, self.t["id"], "cycle_told")
        return json.loads(got[0]) if got else []

    def test_a_lost_wake_records_nothing_and_the_next_check_wakes_once(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        a = self.mine()[0]
        moved = {**a.wait, "line": a.wait["line"] + " "}             # re-declared under the check
        restore = self.race(lambda att, _: self.r.store.update(self.r.store.get(att.id), wait=moved))
        self.r._wake_one(self.r.store.get(a.id))
        restore()
        self.assertEqual(self.r.store.get(a.id).state, "waiting")     # the wait CAS lost: no wake
        self.assertEqual(self.told_marks(), [])                      # and nothing recorded as told
        self.r._wake_one(self.r.store.get(a.id))
        self.assertEqual(self.r.store.get(a.id).state, "ready")      # the next check delivers it
        self.assertEqual(len(self.told_marks()), 1)

    def test_a_cycle_told_by_another_writer_meanwhile_does_not_wake(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        a = self.mine()[0]
        restore = self.race(lambda att, wait: self.assertTrue(self.r._claim_cycle(att, wait["cycle_told"])))
        self.r._wake_one(self.r.store.get(a.id))
        restore()
        self.assertEqual(self.r.store.get(a.id).state, "waiting")    # the claim lost inside the commit
        self.assertEqual(len(self.told_marks()), 1)                  # the other writer's record stands
        self.r._wake_one(self.r.store.get(a.id))
        self.assertEqual(self.r.store.get(a.id).state, "waiting")
        self.assertIn("cycle_wake_suppressed", events(self.r, a.id))

    def test_a_contended_mark_is_not_claimed(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        a = self.mine()[0]
        store = self.r.store
        real = store.add_mark
        store.add_mark = lambda *args: False                       # another writer always wins
        self.addCleanup(setattr, store, "add_mark", real)
        self.assertIsNone(self.r._claim_cycle(a, "0" * 16))
        self.assertEqual(self.told_marks(), [])

    def test_a_wait_told_before_signatures_existed_stays_told(self):
        self.start({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        a = self.mine()[0]
        self.r.store.update(a, wait={**a.wait, "cycle_told": True})
        self.ticks(3)
        self.assertEqual(self.cycle_prompts(), [])

    def test_a_card_wait_without_a_cycle_is_left_alone(self):
        self.start({"kind": "pipeline", "target": "group/app#7", "line": "⏳ WAIT pipeline:group/app#7"})
        self.ticks(3)
        self.assertEqual(self.cycle_prompts(), [])
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "cycle_told"))
        self.assertEqual([a.state for a in self.mine()], ["waiting"])


class DecisionAboutALiveTaskThatWaitsOnTheAsker(Bench):
    """The observed shape: the question names the addressee's live task, and that task waits on the asker."""

    def declare(self, their_wait):
        self.source = task(1)
        self.live = task(2, cat="in_progress", assignee_id="agent-lead")
        self.mesh = FakeMesh([self.source, self.live], max_concurrent=1)
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        if their_wait:
            theirs = self.r.store.create("lead", self.live["id"], "claude", "fixture")
            self.r.store.update(theirs, state="waiting", wait=their_wait)
        write_replies(self.replies, {self.source["id"][:8]:
                                     f"⏳ WAIT decision:@lead: run A5 on prod from #{self.live['id'][:8]}"})
        self.r.tick()
        return self.finish_turn(self.r)

    def test_it_is_filed_as_a_request_not_converted_into_a_cycle(self):
        a = self.declare({"kind": "decision", "handle": "builder",
                          "line": f"⏳ WAIT decision:@builder: run A5 on prod from #{MINE8}"})
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["kind"], "decision")              # not card:#<live>
        self.assertNotIn("converted_from", a.wait)
        self.assertEqual(len(self.mesh.created), 1)               # one addressed request
        self.assertEqual(self.mesh.created[0]["assignee_id"], "agent-lead")
        self.assertIn("decision_to_card_wait_skipped", events(self.r, a.id))
        self.assertNotIn("decision_to_card_wait", events(self.r, a.id))

    def test_the_live_task_waiting_on_this_card_closing_is_a_cycle_too(self):
        a = self.declare({"kind": "card", "target": MINE8, "line": f"⏳ WAIT card:#{MINE8}"})
        self.assertEqual(a.wait["kind"], "decision")
        self.assertIn("decision_to_card_wait_skipped", events(self.r, a.id))

    def test_without_a_cycle_the_question_still_becomes_a_card_wait(self):
        for label, their_wait in (("no attempt", None),
                                  ("waits on a pipeline", {"kind": "pipeline", "target": "g/p#1",
                                                           "line": "⏳ WAIT pipeline:g/p#1"}),
                                  ("asks someone else", {"kind": "decision", "handle": "reviewer",
                                                         "line": "⏳ WAIT decision:@reviewer: ok?"})):
            with self.subTest(label):
                self.setUp()
                a = self.declare(their_wait)
                self.assertEqual((a.wait["kind"], a.wait["target"]), ("card", self.live["id"][:8]))
                self.assertEqual(self.mesh.created, [])
                self.assertIn("decision_to_card_wait", events(self.r, a.id))
                self.tearDown()

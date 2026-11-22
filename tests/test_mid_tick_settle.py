"""A turn that ends inside a long tick gives its capacity reservation back inside that tick.

The settle step runs once, at the start of a lane's tick, and a busy lane's tick took up to
25 minutes (start, resume and slot steps of several minutes each). A turn that ended after the
settle step kept its gate reservation, a capacity slot of the shared pool, until the next tick,
and its unreaped leader (a zombie) still counted as a live process group. Other lanes were
refused the slot meanwhile. Now:

* between the steps of the tick and between the units of its long loops (start, resume, wakes,
  skip states) the reservation of every ended turn goes back, and its leader is reaped;
* the turn itself is still settled by the next tick's settle step: its card is never resumed in
  the tick its turn ended in (a turn the watchdog cut off included), and nothing settled it twice;
* the settle step gives back the reservations of all ended turns before settling any of them,
  because settling one turn can itself take minutes.
"""
import json
import unittest
from unittest import mock

from fakes import HarnessGate, FakeMesh, slow, task, wait_time, write_replies
from test_group_alive_zombie import members, wait_for
from test_runner import Bench
from mesh_runner import progress
from mesh_runner.adapters.base import group_alive

MINUTE = 60.0


class MidTickSettle(Bench):
    def setUp(self):
        super().setUp()
        # mock: external boundary — the operator's gate hook (server reservation reserve/admit/end) logs its calls
        self.gate_log = self.root / "gate.log"
        hook = self.root / "hook.sh"
        hook.write_text(f'#!/bin/sh\necho "$MESH_RUNNER_PHASE $MESH_RUNNER_TASK" >> {self.gate_log}\n'
                        'echo RES_ID=r1\n')
        hook.chmod(0o755)
        self.agent.start_gate_command = [str(hook)]
        self.agent.turn_end_command = [str(hook)]

    def released(self, t) -> int:
        """How many times the card's reservation was given back (``end`` calls of the gate hook)."""
        if not self.gate_log.exists():
            return 0
        return self.gate_log.read_text().splitlines().count(f"end {t['id']}")

    def held_turns(self, n=1, max_concurrent=None):
        """``n`` running turns, each held inside its harness until the test ends it."""
        cards = [task(i) for i in range(1, n + 1)]
        mesh = FakeMesh(cards, max_concurrent=max_concurrent or n)
        harnesses = {t["id"]: HarnessGate(self.root / f"turn-{t['id'][:8]}") for t in cards}
        write_replies(self.replies, {t["id"][:8]: harnesses[t["id"]].reply(wait_time(why="after the turn"))
                                     for t in cards})
        r = self.runner(mesh)
        r.tick()
        running = {a.task_id: a for a in r.store.by_state(self.agent.name, ("running",))}
        self.assertEqual(sorted(running), sorted(t["id"] for t in cards))
        for h in harnesses.values():
            wait_for(h.started.exists, "the harness to start its turn")     # tearDown stops the held ones
        return r, mesh, cards, [running[t["id"]] for t in cards], [harnesses[t["id"]] for t in cards]

    def end_turn(self, r, a, harness):
        """The harness ends its turn and nobody reaps it: the leader stays a zombie of the runner."""
        harness.release()
        proc = r.procs[a.id]
        wait_for(lambda: members(a.pid) == [(a.pid, "Z")], "the turn's leader to be a zombie")
        self.assertIsNone(proc.returncode)

    def test_a_turn_ending_in_the_first_minute_of_a_five_minute_step_gives_its_slot_back_in_that_step(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        for n in range(2, 7):                                      # five cards to start: one minute each
            mesh.tasks[task(n)["id"]] = task(n)
        mesh.max_concurrent = 6
        clock, seen = [0.0], []

        def start_one(t, **kwargs):
            clock[0] += MINUTE
            if clock[0] == MINUTE:
                self.end_turn(r, a, harness)                       # the turn ends in the first minute
            seen.append((clock[0] / MINUTE, self.released(t1), r.store.get(a.id).state))
            return False                                           # nothing started: the step goes on

        # mock: the start of one card is the unit of the step; only its duration is simulated
        with mock.patch.object(progress, "_clock", lambda: clock[0]), \
                mock.patch.object(r, "_start_one", side_effect=start_one):
            progress.bind(None, 0)
            try:
                r.tick()
            finally:
                steps = progress.unbind()
        self.assertEqual(steps.get("start"), 5 * MINUTE)           # a five-minute start step
        self.assertEqual([m for m, _, _ in seen], [1, 2, 3, 4, 5])
        self.assertEqual(seen[0][1:], (0, "running"))              # ended in minute 1 ...
        self.assertEqual(seen[1][1:], (1, "running"))              # ... its slot is back at minute 2
        self.assertEqual({rel for _, rel, _ in seen[1:]}, {1})     # given back once
        self.assertEqual(r.store.get(a.id).state, "running")       # the next tick's settle step settles it
        self.assertFalse(group_alive(a.pid, a.stamp))
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(self.released(t1), 1)                     # settle did not release it again
        self.assertNotIn(a.id, r.procs)

    def test_a_zombie_leader_is_reaped_and_gives_its_slot_back_in_the_tick_it_ended_in(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        at_publish = []
        real_publish = r.publish_skips

        def slots():                                               # the turn ends during the slots step
            self.end_turn(r, a, harness)

        def publish():
            at_publish.append((r.store.get(a.id).state, self.released(t1)))
            real_publish()
        with mock.patch.object(r, "recheck_slots", side_effect=slots), \
                mock.patch.object(r, "publish_skips", side_effect=publish):
            r.tick()
        self.assertEqual(at_publish, [("running", 1)])             # slot back before the next step ran
        self.assertFalse(members(a.pid))                           # reaped: no zombie left behind
        r.settle_finished()                                        # the next tick's settle step
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("waiting", 1))

    def test_the_settle_step_gives_back_every_ended_turns_slot_before_settling_any(self):
        r, mesh, (t1, t2), (a1, a2), (h1, h2) = self.held_turns(2)
        self.end_turn(r, a1, h1)
        self.end_turn(r, a2, h2)
        at_settle = []
        real_settle = r.settle

        def settle(a):                                             # settling one turn may take minutes
            at_settle.append((a.task_id, self.released(t1), self.released(t2)))
            real_settle(a)
        with mock.patch.object(r, "settle", side_effect=settle):
            r.settle_finished()
        self.assertEqual(len(at_settle), 2)
        self.assertEqual({(rel1, rel2) for _, rel1, rel2 in at_settle}, {(1, 1)})
        self.assertEqual({r.store.get(a1.id).state, r.store.get(a2.id).state}, {"waiting"})
        self.assertEqual((self.released(t1), self.released(t2)), (1, 1))

    def test_a_running_turn_keeps_its_slot_through_the_passes(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()      # positive control: the turn still runs
        r.tick()
        r._between()
        r._between(step=True)
        self.assertEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.released(t1), 0)
        self.assertTrue(group_alive(a.pid, a.stamp))

    def test_a_child_outliving_the_leader_keeps_the_slot_until_it_ends(self):
        t1 = task(1)
        mesh = FakeMesh([t1], max_concurrent=1)
        child = HarnessGate(self.root / "background")
        self.agent.inline_env["FAKE_BG_GATE"] = str(child.path)
        write_replies(self.replies, {t1["id"][:8]: slow("BG:GATE:", wait_time(why="after the turn"))})
        r = self.runner(mesh)
        r.tick()
        (a,) = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(r.procs[a.id].wait(timeout=10), 0)       # the leader is gone (reaped) ...
        wait_for(child.started.exists, "the background child to start")
        r._between(step=True)
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("running", 0))   # ... its child writes on
        child.release()
        wait_for(lambda: not members(a.pid), "the background child to end")
        r._between()
        self.assertEqual(self.released(t1), 1)
        r._between(step=True)
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("running", 1))
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(self.released(t1), 1)

    def test_an_ended_turn_without_proof_of_delivery_keeps_its_slot_for_the_settle_step(self):
        t1 = task(1)
        mesh = FakeMesh([t1], max_concurrent=1)
        # the harness exits before it accepts the prompt: nothing proves the prompt was taken
        write_replies(self.replies, {t1["id"][:8]: slow("DIE:refused before the stream\n", wait_time(why="x"))})
        r = self.runner(mesh)
        r.tick()
        (a,) = r.store.by_state(self.agent.name, ("running",))
        # premise: the turn is over: its leader a zombie, or already reaped by a pass of the tick ...
        wait_for(lambda: members(a.pid) in ([], [(a.pid, "Z")]), "the turn's leader to be gone or a zombie")
        self.assertTrue(r._turn_ended(r.store.get(a.id)))
        self.assertEqual(self.released(t1), 0)                     # ... yet no pass of the tick gave the slot back
        r._between()
        self.assertEqual(self.released(t1), 0)                     # ... only the settle step decides
        r._between(step=True)
        self.assertEqual(self.released(t1), 0)
        r.settle_finished()
        held = r.store.get(a.id)
        self.assertEqual(held.state, "unknown")                    # it holds the card: so does the slot
        self.assertEqual((self.released(t1), r._gate_of(held)), (0, {"RES_ID": "r1"}))

    def as_interactive(self, r, a, acked: bool):
        """The turn's prompt intent as an interactive driver records it; ``acked``: the driver wrote its
        durable end-of-turn acknowledgement into the turn's log (an interactive lane's ordinary end)."""
        current = r.store.get(a.id)
        intent = dict(current.wait["interactive_prompt_intent"], kind="interactive")
        self.assertTrue(r.store.update(current, wait={**current.wait, "interactive_prompt_intent": intent}))
        if acked:
            with open(intent["log_path"], "a") as fh:
                fh.write(json.dumps({"type": "mesh_runner.prompt_ack", "token": intent["token"],
                                     "sha256": intent["sha256"]}) + "\n")
        return r.store.get(a.id)

    def test_an_interactive_turn_acknowledged_by_its_driver_gives_its_slot_back_early(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        self.end_turn(r, a, harness)
        self.as_interactive(r, a, acked=True)
        r._between()
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("running", 1))
        r.settle_finished()
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("waiting", 1))

    def test_an_interactive_turn_without_its_drivers_ack_keeps_its_slot_and_is_read_once(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        self.end_turn(r, a, harness)
        self.as_interactive(r, a, acked=False)
        with mock.patch.object(r, "_settles_on_its_ack", wraps=r._settles_on_its_ack) as read:
            for _ in range(3):                                     # three units of a long loop
                r._between()
            self.assertEqual((read.call_count, self.released(t1)), (1, 0))
            r._release_ended(fresh=True)                           # the next settle pass reads it again
            self.assertEqual((read.call_count, self.released(t1)), (2, 0))

    def test_a_turn_ending_while_another_settles_gives_its_slot_back_before_its_own_settle(self):
        r, mesh, cards, attempts, harnesses = self.held_turns(2)
        order = [x.id for x in r.store.by_state(self.agent.name, ("running",))]
        first, second = ([x.id for x in attempts].index(aid) for aid in order)
        self.end_turn(r, attempts[first], harnesses[first])
        at_settle = []
        real_settle = r.settle

        def settle(a):                                             # the first settle takes minutes ...
            if a.id == attempts[first].id:
                self.end_turn(r, attempts[second], harnesses[second])   # ... and the other turn ends meanwhile
            at_settle.append((a.id, self.released(cards[second])))
            real_settle(a)
        with mock.patch.object(r, "settle", side_effect=settle):
            r.settle_finished()
        self.assertEqual(at_settle, [(attempts[first].id, 0), (attempts[second].id, 1)])
        self.assertEqual(self.released(cards[second]), 1)

    def test_no_pass_acts_before_the_ticks_own_settle_step(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        calls = []
        real_terminal, real_settle = r.settle_terminal_intents, r.settle

        def terminal():
            calls.append("settle step")
            real_terminal()

        def settle(x):
            calls.append("settle " + x.task_id[:8])
            real_settle(x)
        # the turn ends during the refresh, the first step of the next tick
        with mock.patch.object(r, "heartbeat", side_effect=lambda: self.end_turn(r, a, harness)), \
                mock.patch.object(r, "settle_terminal_intents", side_effect=terminal), \
                mock.patch.object(r, "settle", side_effect=settle):
            r.tick()
        self.assertEqual(calls[:2], ["settle step", "settle " + t1["id"][:8]])
        self.assertEqual(r.store.get(a.id).state, "waiting")

    def test_a_turn_the_watchdog_cut_off_is_settled_by_the_next_tick_not_this_one(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        self.cfg.turn_idle_minutes = 0.0001                        # the silent turn is past its idle limit
        resumed = []
        real_resume = r.resume_ready

        def resume(room):
            resumed.append((r.store.get(a.id).state, r.store.get(a.id).turns))
            return real_resume(room)
        try:
            with mock.patch.object(r, "resume_ready", side_effect=resume):
                r.tick()                                           # its settle step fires the watchdog
        finally:
            self.cfg.turn_idle_minutes = 0
        self.assertIn("watchdog", [k for _, k, _ in r.store.events(a.id)])
        self.assertFalse(group_alive(a.pid, a.stamp))              # the forced end is complete ...
        self.assertEqual(resumed, [("running", a.turns)])          # ... yet no pass of this tick settled it
        self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).turns), ("running", a.turns))
        self.assertEqual(self.released(t1), 1)                     # its slot is back in this tick
        r.settle_finished()                                        # the next tick's settle step settles it
        self.assertNotEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.released(t1), 1)                     # without a second release

    def test_settle_does_not_give_a_returned_reservation_back_again(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        self.end_turn(r, a, harness)
        r._between()                                               # given back early ...
        r.settle_finished()                                        # ... settled without a second release
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("waiting", 1))
        self.assertEqual(r._gate_returned, set())                  # nothing left to skip on a later turn


if __name__ == "__main__":
    unittest.main()

"""A slot refusal at admit that the start gate turned into an overflow request
(``TASK_ROUTE_OVERFLOW=requested``) is fed again at once, in the same call: the next reserve
takes the next profile of the card's chain. Once per call, never a loop; a refusal without the
key keeps the ordinary hold."""
import json
import sys

from fakes import FakeMesh, calls, task, wait_time, write_replies
from test_runner import Bench
from mesh_runner.runner import OVERFLOW_KEY, overflow_requested

SLOT_REFUSAL = "capacity: runtime.capacity: server admit hold acquire_refused:pool_exhausted"


class OverflowFeedNow(Bench):
    def _gate(self, refusals: int, overflow: bool = True):
        """A gate whose first `refusals` admits are slot refusals; the log names every phase."""
        state, log_file = self.root / "gate-state.json", self.root / "hooklog"
        state.write_text(json.dumps({"left": refusals}))
        key = f"print('{OVERFLOW_KEY}=requested')\n" if overflow else ""
        gate = ("import os,sys,json,pathlib\n"
                f"st=pathlib.Path({str(state)!r}); s=json.loads(st.read_text())\n"
                "ph=os.environ['MESH_RUNNER_PHASE']\n"
                f"pathlib.Path({str(log_file)!r}).open('a').write(ph+'\\n')\n"
                "if ph=='admit' and s['left']>0:\n"
                " s['left']-=1; st.write_text(json.dumps(s))\n"
                f" print({SLOT_REFUSAL!r})\n"
                + "".join(" " + line + "\n" for line in key.splitlines()) +
                " sys.exit(75)\n"
                "print('RES=r1')\n"
                "if ph=='admit': print('ADMITTED=1')\n")
        end = (f"import os,pathlib\npathlib.Path({str(log_file)!r}).open('a').write('end '+"
               "os.environ['MESH_RUNNER_OUTCOME']+'\\n')\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        self.agent.turn_end_command = [sys.executable, "-c", end]
        return log_file

    def phases(self, log_file):
        return log_file.read_text().split()

    def test_key_is_read_only_as_requested(self):
        self.assertTrue(overflow_requested({OVERFLOW_KEY: "requested"}))
        self.assertFalse(overflow_requested({OVERFLOW_KEY: "1"}))
        self.assertFalse(overflow_requested({}))
        self.assertFalse(overflow_requested(None))

    def test_start_refused_on_full_slots_is_fed_again_in_the_same_tick(self):
        t = task(1)
        log_file = self._gate(refusals=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        self.assertEqual(len(r.store.by_state("builder", ("running",))), 1)   # started in this tick
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(self.phases(log_file)[:5], ["reserve", "admit", "end", "aborted", "reserve"])
        self.assertIn("admit", self.phases(log_file)[5:])
        self.assertFalse(r._start_held)
        self.assertTrue(any("overflow fed now" in line for line in logs.output))

    def test_start_refusal_without_the_key_keeps_the_ordinary_hold(self):
        t = task(1)
        log_file = self._gate(refusals=1, overflow=False)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(self.phases(log_file).count("reserve"), 1)
        self.assertTrue(r._start_held)
        self.assertTrue(r.hold_reason(t, "work").startswith("gate: runtime capacity unavailable"))

    def test_start_overflow_is_fed_once_never_in_a_loop(self):
        t = task(1)
        log_file = self._gate(refusals=5)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(self.phases(log_file).count("admit"), 2)       # the refusal and one overflow feed
        self.assertTrue(r._start_held)
        self.assertTrue(r.hold_reason(t, "work").startswith("gate: runtime capacity unavailable"))

    # The lifecycle event of the attempt a slot refusal superseded is written once the overflow
    # feed has answered: a lane that has an open profile is not on record as held for capacity,
    # a lane that has none still is, exactly once.
    def _events(self):
        path = self.cfg.state_dir / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _capacity_events(self):
        return [e for e in self._events() if "runtime capacity unavailable" in (e.get("note") or "")]

    def test_start_fed_on_overflow_is_not_on_record_as_a_capacity_hold(self):
        t = task(1)
        self._gate(refusals=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual(len(r.store.by_state("builder", ("running",))), 1)
        self.assertEqual(self._capacity_events(), [])
        aborted = [e for e in self._events() if e["kind"] == "aborted"]
        self.assertEqual(len(aborted), 1)                     # the superseded attempt, once
        self.assertIn("overflow", aborted[0]["note"])
        self.assertEqual(len([e for e in self._events() if e["kind"] == "start"]), 1)

    def test_start_overflow_refused_again_is_one_capacity_hold(self):
        t = task(1)
        self._gate(refusals=5)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(len(self._capacity_events()), 1)    # the overflow feed's own refusal
        self.assertEqual(len([e for e in self._events() if e["kind"] == "aborted"]), 2)

    def test_start_overflow_held_at_reserve_is_one_capacity_hold(self):
        t = task(1)
        log_file = self._gate(refusals=1)
        # the overflow feed finds no open profile: its reserve holds, no second attempt is made
        cmd = self.agent.start_gate_command[-1]
        cmd = cmd.replace(" s['left']-=1;", " s['left']-=1; s['fed']=1;")
        cmd = cmd.replace("print('RES=r1')\n",
                          "if ph=='reserve' and s.get('fed'):\n"
                          " print('route: slots_fallback:fallback_no_available_route'); sys.exit(75)\n"
                          "print('RES=r1')\n")
        self.agent.start_gate_command[-1] = cmd
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(self.phases(log_file).count("reserve"), 2)
        self.assertEqual(self.phases(log_file).count("admit"), 1)
        held = self._capacity_events()
        self.assertEqual(len(held), 1)                        # the superseded attempt keeps the hold
        self.assertEqual(held[0]["kind"], "aborted")
        self.assertEqual(len([e for e in self._events() if e["kind"] == "aborted"]), 1)

    def _waiting(self):
        t = task(1)
        log_file = self._gate(refusals=0)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window"),
                                     t["id"][:8] + ":resume": wait_time(why="soak window")})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        r.store.update(a, state="ready", wake_text="go on")
        log_file.write_text("")
        return r, a, log_file

    def _refuse(self, refusals, overflow=True):
        state = self.root / "gate-state.json"
        state.write_text(json.dumps({"left": refusals}))
        if not overflow:
            self.agent.start_gate_command[-1] = self.agent.start_gate_command[-1].replace(
                f" print('{OVERFLOW_KEY}=requested')\n", "")

    def test_resume_refused_on_full_slots_is_fed_again_in_the_same_call(self):
        r, a, log_file = self._waiting()
        self._refuse(1)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r._resume_one(r.store.get(a.id))
        self.assertEqual(self.phases(log_file)[:6], ["reserve", "admit", "end", "held", "reserve", "admit"])
        self.assertEqual(r.store.get(a.id).state, "running")         # resumed now, not next tick
        self.settle_all(r)
        resumed = calls(self.calls)[1]
        self.assertTrue(resumed["resume"])
        self.assertTrue(resumed["prompt"].startswith("go on"))
        self.assertNotIn(a.id, r._resume_hold)
        self.assertTrue(any("overflow fed now" in line for line in logs.output))

    def test_resume_refusal_without_the_key_is_held_as_before(self):
        r, a, log_file = self._waiting()
        self._refuse(1, overflow=False)
        r._resume_one(r.store.get(a.id))
        self.assertEqual(self.phases(log_file).count("reserve"), 1)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertIn(a.id, r._resume_hold)

    def test_resume_overflow_is_fed_once_never_in_a_loop(self):
        r, a, log_file = self._waiting()
        self._refuse(5)
        r._resume_one(r.store.get(a.id))
        self.assertEqual(self.phases(log_file).count("admit"), 2)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertIn(a.id, r._resume_hold)
        self.assertEqual(r.store.get(a.id).checkout, {})              # no checkout left behind

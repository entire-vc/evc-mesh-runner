"""A turn whose driver gave up after the paste leaves its prompt delivery unproven (``unknown``). Such attempts
used to stand until an operator token ended them. Now:

- the transition signals the operator once and tells the card in plain words what happens next;
- once the turn's process group is gone and its native evidence stays unchanged for UNKNOWN_QUIET_MINUTES, the
  runner ends the attempt the way an operator token does: the prompt is archived, never replayed, and the card
  starts in a new attempt; a live process or changing evidence keeps it;
- a driver claims its window under the host start lock, so another driver's make_room cannot close the window of
  a turn that has begun; a window whose prompt went in and whose driver is gone is never closed as idle;
- no window starts while the host is under the memory floor, read right before the window (not a cached reading).
"""
import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
from unittest import mock
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner import host_admission, hostmem, interactive, runner as runner_mod, tui_driver
from mesh_runner.adapters.base import interrupt_group, proc_stamp
from test_runner import Bench


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


class UnknownAfterPaste(Bench):
    """The incident's exact state: journal at ``send-intent`` (pasted), no UserPromptSubmit, driver gone."""

    def setUp(self):
        super().setUp()
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.children = []
        a = self.r.store.create(self.agent.name, self.t["id"], "claude", self.t["title"])
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        log = self.root / f"{a.id}.turn2.claude.jsonl"
        log.write_text(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                   "result": "the session did not take the prompt: ..."}) + "\n")
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "sha256": "2" * 64, "turn": 2,
                       "kind": "interactive", "harness": "claude", "log_path": str(log), "prompt": "PROMPT",
                       "session_id": str(uuid.uuid4()), "source_read_at": "", "resume": True,
                       "prior": {"turns": 1, "session_id": "", "log_path": "", "turn_started_at": "",
                                 "source_read_at": ""}}
        interactive.write_delivery(self.directory, a.id, {
            "version": 1, "token": self.intent["token"], "sha256": self.intent["sha256"], "phase": "send-intent",
            "harness": "claude", "session_id": self.intent["session_id"], "cwd": str(self.root),
            "native_base": 0, "native_prefix": hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest(),
            "offset": 0, "meta": {}})
        pid, stamp = dead_pid()
        self.assertTrue(self.r.store.update(a, expect="starting", state="running", pid=pid, stamp=stamp, turns=2,
                                            log_path=str(log), wait={"interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                interrupt_group(p.pid, 9)
            p.wait(timeout=5)
        super().tearDown()

    def alerts(self):
        path = self.cfg.state_dir / "events.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        return [r for r in rows if r["kind"] == "operator_alert"]

    def comments(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])]

    def age_quiet_clock(self, minutes):
        key = "unknown_quiet:" + self.a.id
        mark = json.loads(self.r.store.mark(self.agent.name, self.t["id"], key)[0])
        mark["since"] -= minutes * 60
        self.r.store.mark(self.agent.name, self.t["id"], key, json.dumps(mark))

    def test_unknown_signals_once_and_explains_itself(self):
        for _ in range(3):
            self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(a.wait["interactive_prompt_intent"]["resolution"], "acceptance-unproven")
        self.assertEqual(len(self.alerts()), 1, "one operator signal per event, not per tick")
        self.assertIn(self.a.id, self.alerts()[0]["alert"])
        held = [c for c in self.comments() if "MANDATORY DELIVERY PENDING" in c]
        self.assertEqual(len(held), 1)
        self.assertIn("did not confirm that it received the last prompt", held[0])
        self.assertIn("is not sent again", held[0])
        self.assertIn(f"in {runner_mod.UNKNOWN_QUIET_MINUTES} min", held[0])

    def test_dead_driver_quiet_for_n_minutes_ends_the_attempt_without_replay(self):
        self.r.settle_finished()                          # -> unknown, quiet clock starts
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES - 1)
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown", "not before N minutes")
        self.age_quiet_clock(2)
        self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "completed")
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertIn("quiet_resolution", [k for _, k, _ in self.r.store.events(a.id)])
        archived = json.loads(self.r.store.mark(self.agent.name, self.t["id"], "operator_resolved_intent:" + a.id)[0])
        self.assertEqual(archived["intent"]["token"], self.intent["token"])
        self.assertEqual(self.r.store.terminal_with_intent(self.agent.name), [])
        # the card is free for a new attempt; nothing carries the old prompt
        for other in self.r.store.by_state(self.agent.name, ("starting", "running", "ready", "waiting", "unknown")):
            self.assertNotEqual((other.wait or {}).get("interactive_prompt_intent", {}).get("token"),
                                self.intent["token"])
        self.assertTrue(any("The held turn is closed" in c and "not sent again" in c for c in self.comments()))

    def test_changing_evidence_restarts_the_quiet_clock(self):
        self.r.settle_finished()
        self.r.settle_finished()
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES + 5)
        with open(self.directory / f"{self.a.id}.events.jsonl", "a") as fh:
            fh.write(json.dumps({"hook_event_name": "SessionStart", "session_id": self.intent["session_id"]}) + "\n")
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")

    def test_an_unreadable_evidence_read_is_no_proof_of_quiet(self):
        self.r.settle_finished()
        self.r.settle_finished()
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES + 5)
        changing = mock.patch.object(runner_mod.delivery_requests, "evidence",
                                     side_effect=ValueError("native-evidence-changing"))
        with changing:
            for _ in range(3):                            # the evidence keeps changing for longer than N minutes
                self.r.settle_finished()
                mark = self.r.store.mark(self.agent.name, self.t["id"], "unknown_quiet:" + self.a.id)
                if mark and mark[0]:
                    self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES + 5)
            self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown", "a failed read never expires the clock")
        self.r.settle_finished()                          # readable again: the clock restarts from now
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES + 1)
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "completed")

    def test_a_live_process_keeps_the_attempt(self):
        self.r.settle_finished()
        self.r.settle_finished()
        self.age_quiet_clock(runner_mod.UNKNOWN_QUIET_MINUTES + 5)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.children.append(p)
        a = self.r.store.get(self.a.id)
        self.assertTrue(self.r.store.update(a, pid=p.pid, stamp=proc_stamp(p.pid)))
        for _ in range(2):
            self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.assertNotIn("quiet_resolution", [k for _, k, _ in self.r.store.events(self.a.id)])


class FakeTmux:
    def __init__(self, windows):
        self.windows, self.killed = list(windows), []

    def all_windows(self):
        return list(self.windows)

    def kill_window(self, session, window):
        self.killed.append((session, window))

    def run(self, *args, **kw):
        return subprocess.CompletedProcess(args, 1, "", "")


class WindowClaim(Bench):
    def driver(self, key, tmux, cap=1, **kw):
        d = tui_driver.Driver(harness="claude", agent="builder", state=self.root / "interactive" / "builder",
                              tmux=tmux, max_sessions=cap, cwd=self.root, session_id="", mode="resume", key=key,
                              argv=["claude"], env={}, **kw)
        return d

    def test_a_claim_waits_for_the_start_lock_and_then_protects_the_window(self):
        tmux = FakeTmux([("mr-builder", "turn-a")])
        a = self.driver("turn-a", tmux)
        a.private_dir()
        interactive.write_meta(a.dir, "turn-a", {"busy_pid": 0, "last_end": 1.0})
        done = threading.Event()
        with host_admission.start_lock(a.dir.parent):        # another driver counts and evicts right now
            th = threading.Thread(target=lambda: (a.claim_window(), done.set()))
            th.start()
            self.assertFalse(done.wait(0.3), "the claim must not land inside another driver's make_room")
            self.assertEqual(interactive.read_meta(a.dir, "turn-a")["busy_pid"], 0)
        th.join(5)
        self.assertTrue(done.is_set())
        other = self.driver("turn-b", tmux)
        with host_admission.start_lock(a.dir.parent):
            self.assertFalse(other.make_room(), "the claimed window of a begun turn is not idle")
        self.assertEqual(tmux.killed, [])

    def test_a_window_with_an_unresolved_paste_is_not_closed_as_idle(self):
        tmux = FakeTmux([("mr-builder", "turn-a")])
        a = self.driver("turn-a", tmux)
        a.private_dir()
        interactive.write_meta(a.dir, "turn-a", {"busy_pid": 0, "last_end": 1.0})
        interactive.write_delivery(a.dir, "turn-a", {"version": 1, "token": "t", "sha256": "0" * 64,
                                                     "phase": "send-intent"})
        self.assertFalse(self.driver("turn-b", tmux).make_room())
        self.assertEqual(tmux.killed, [])
        interactive.write_delivery(a.dir, "turn-a", {"version": 1, "token": "t", "sha256": "0" * 64,
                                                     "phase": "accepted"})
        self.assertTrue(self.driver("turn-b", tmux).make_room())
        self.assertEqual(tmux.killed, [("mr-builder", "turn-a")])


class MemoryFloor(Bench):
    def test_no_window_starts_under_the_memory_floor(self):
        clock = [0.0]
        created = []
        reads = []

        def reader():
            reads.append(clock[0])
            return 10.0, 72.0                     # 10 % free, 72 MB swap free: 2026-11-11 22:28
        d = tui_driver.Driver(harness="claude", agent="builder", state=self.root / "interactive" / "builder",
                              tmux=FakeTmux([]), max_sessions=22, cwd=self.root, session_id="", mode="start",
                              key="turn-a", argv=["claude"], env={}, clock=lambda: clock[0],
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                              memory_floor=(15.0, 300.0), memory_reader=reader)
        d.new_window = lambda cmd: created.append(cmd)
        d.wait_ready = lambda: None
        d.beat = lambda *a, **k: None
        with self.assertRaises(tui_driver.DriverError) as e:
            d.start_window()
        self.assertIn("host short of memory", str(e.exception))
        self.assertEqual(created, [])
        self.assertGreater(len(reads), 2, "read at every attempt, under the lock, not cached")

    def test_a_window_starts_when_memory_is_back(self):
        state = {"free": 10.0, "swap": 72.0}
        created = []
        d = tui_driver.Driver(harness="claude", agent="builder", state=self.root / "interactive" / "builder",
                              tmux=FakeTmux([]), max_sessions=22, cwd=self.root, session_id="", mode="start",
                              key="turn-a", argv=["claude"], env={}, clock=time.time,
                              sleep=lambda s: state.update(free=40.0, swap=900.0),
                              memory_floor=(15.0, 300.0), memory_reader=lambda: (state["free"], state["swap"]))
        d.new_window = lambda cmd: created.append(cmd)
        d.wait_ready = lambda: None
        d.beat = lambda *a, **k: None
        d.start_window()
        self.assertEqual(len(created), 1)

    def test_the_runner_gate_reads_fresh_before_a_spawn(self):
        values = iter([(50.0, 5000.0), (10.0, 72.0)])
        clock = [0.0]
        gate = hostmem.MemoryGate(hostmem.Thresholds(15, 20, 300, 600), reader=lambda: next(values),
                                  clock=lambda: clock[0])
        self.assertFalse(gate.check())
        self.assertFalse(gate.check(), "cached for 30 s")
        self.assertTrue(gate.check(fresh=True), "the spawn's look reads the host now")

    def test_short_now_is_stateless_and_fails_open(self):
        self.assertEqual(hostmem.short_now(0, 300, lambda: (1.0, 1.0)), "")
        self.assertEqual(hostmem.short_now(15, 300, lambda: (50.0, 72.0)), "")   # swap counts only when free is low
        self.assertIn("swap free 72MB", hostmem.short_now(15, 300, lambda: (10.0, 72.0)))
        self.assertEqual(hostmem.short_now(15, 300, lambda: (None, None)), "")

    def test_the_driver_gets_the_floor_on_its_command_line(self):
        base = self.adapter
        wrapped = interactive.InteractiveAdapter(base, "builder", self.cfg.state_dir, memory_floor=(15.0, 300.0)) \
            if hasattr(base, "interactive_argv") else None
        if wrapped is None:
            self.skipTest("fake adapter has no interactive mode")
        argv = wrapped.start_argv("", self.root, [])
        head = argv[:argv.index("--")]
        self.assertEqual(head[head.index("--memory-hold-swap-mb") + 1], "300")
        opts, _ = tui_driver.parse_args(head[2:] + ["--key", "k", "--", "x"])
        self.assertEqual(opts["memory_hold_free_pct"], "15")

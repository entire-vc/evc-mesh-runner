"""A turn whose prompt was never sent kept its consumed capacity slot.

Production, 24.11: one lane's turns on six cards ended ``prompt_unsent_reconciled`` (the native process
gone, the attempt back to ``ready`` with pid 0) a few minutes after each launch. That path released the
start gate's reservation only for a quota refusal, so the ``gate`` mark and its consumed server
reservation outlived the turn. Six dead slots filled the lane's identity cap of 6 (another lane: 1 of 1);
every later admit was refused ``identity_cap_reached``, the lane-wide capacity hold kept the six holders'
own resumes from asking the gate again, and the lane stood.

The unsent path now releases the gate like every other end of a turn, and each tick releases a gate
still held by an attempt that has no turn (ready or waiting, no process): the slots stranded before this
fix are freed by the fix itself.
"""
import hashlib
import json
import subprocess
import sys
import uuid
from unittest.mock import patch

from fakes import FakeMesh, task
from mesh_runner import interactive
from mesh_runner.adapters.base import proc_stamp
from mesh_runner.runner import GATE_RELEASE_PENDING
from test_runner import Bench

GATE = {"RES_ID": "res-stale", "RES_PROFILE": "claude/team/subscription", "RES_BACKEND": "server",
        "RES_ADMITTED": "1", "RES_CONSUMED": "1", "RES_GENERATION": "21"}


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


class StaleGate(Bench):
    def setUp(self):
        super().setUp()
        self.agent.start_gate_command = ["operator-gate"]
        self.agent.turn_end_command = ["operator-end"]
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.r.me = self.mesh.me()
        self.hooks = []
        self.confirm = True

    def hook(self, argv, env, timeout):
        self.hooks.append((env.get("MESH_RUNNER_PHASE"), env.get("MESH_RUNNER_GATE_RES_ID"),
                           env.get("MESH_RUNNER_GATE_RES_GENERATION"), env.get("MESH_RUNNER_OUTCOME")))
        return (0, "RELEASE_CONFIRMED=1\n" if self.confirm else "", "")

    def gate_mark(self):
        row = self.r.store.mark(self.agent.name, self.t["id"], "gate")
        return row[0] if row else ""

    def pending(self):
        row = self.r.store.mark(self.agent.name, self.t["id"], GATE_RELEASE_PENDING)
        return json.loads(row[0]) if row and row[0] else []

    def attempt(self, state, pid=0, stamp=""):
        a = self.r.store.create(self.agent.name, self.t["id"], "claude", self.t["title"])
        self.assertTrue(self.r.store.update(a, state=state, pid=pid, stamp=stamp, turns=3))
        self.r.store.mark(self.agent.name, self.t["id"], "gate", json.dumps(GATE))
        return self.r.store.get(a.id)

    # ------------------------------------------------------------- the unsent end of a turn

    def unsent_turn(self):
        """A running claude turn whose driver is gone and whose delivery journal never got past
        ``reserved``: the exact prompt was never pasted."""
        pid, stamp = dead_pid()
        a = self.attempt("running", pid=pid, stamp=stamp)
        directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        directory.mkdir(parents=True, exist_ok=True)
        log = self.root / f"{a.id}.turn3.claude.jsonl"
        log.write_text("")
        prompt = "EXACT PENDING REQUEST"
        intent = {"version": 1, "token": uuid.uuid4().hex, "turn": 3, "log_path": str(log),
                  "sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt": prompt,
                  "kind": "interactive", "harness": "claude", "session_id": "", "workspaces": [],
                  "source_read_at": "pending-source",
                  "prior": {"turns": 2, "session_id": "", "log_path": "", "turn_started_at": "",
                            "source_read_at": "previous-source"}}
        interactive.write_delivery(directory, a.id, {"version": 1, "token": intent["token"],
                                                     "sha256": intent["sha256"], "phase": "reserved"})
        self.assertTrue(self.r.store.update(a, log_path=str(log), wait={"interactive_prompt_intent": intent}))
        return self.r.store.get(a.id)

    def test_an_unsent_turn_releases_its_consumed_slot(self):
        a = self.unsent_turn()
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.assertFalse(self.r._reconcile_interactive_prompt(a))
        a = self.r.store.get(a.id)
        self.assertEqual((a.state, a.pid), ("ready", 0))
        self.assertIn("prompt_unsent_reconciled", [k for _, k, _ in self.r.store.events(a.id)])
        # the end hook got exactly the reservation the turn's gate held, for its writer
        self.assertEqual(self.hooks, [("end", "res-stale", "21", "pre-send-unsent")])
        self.assertEqual(self.gate_mark(), "")
        self.assertEqual(self.pending(), [])
        # the exact prompt is still kept for the next normal admission
        self.assertTrue(a.wait["interactive_prompt_intent"]["retryable"])

    def test_an_unconfirmed_unsent_release_stays_pending(self):
        self.confirm = False
        a = self.unsent_turn()
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.r._reconcile_interactive_prompt(a)
        self.assertEqual(self.r.store.get(a.id).state, "ready")
        self.assertEqual([p["gate"]["RES_ID"] for p in self.pending()], ["res-stale"])

    # ------------------------------------------------------------- slots stranded before the fix

    def test_a_ready_attempt_without_a_process_gives_back_a_stranded_gate(self):
        a = self.attempt("ready")
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.r.release_stale_gates()
            self.r.release_stale_gates()                 # once: the mark is gone after the first
        self.assertEqual(self.hooks, [("end", "res-stale", "21", "stale-gate")])
        self.assertEqual(self.gate_mark(), "")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.r.store.get(a.id).state, "ready")

    def test_a_waiting_attempt_with_a_dead_process_gives_back_a_stranded_gate(self):
        pid, stamp = dead_pid()
        self.attempt("waiting", pid=pid, stamp=stamp)
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.r.release_stale_gates()
        self.assertEqual([h[1] for h in self.hooks], ["res-stale"])
        self.assertEqual(self.gate_mark(), "")

    def test_an_unconfirmed_stranded_release_is_retried_not_dropped(self):
        self.confirm = False
        self.attempt("ready")
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.r.release_stale_gates()
        self.assertEqual(self.gate_mark(), "")
        self.assertEqual([p["gate"]["RES_ID"] for p in self.pending()], ["res-stale"])

    def test_a_turn_in_progress_keeps_its_gate(self):
        for state in ("running", "starting", "unknown"):
            with self.subTest(state=state):
                self.r.store.mark(self.agent.name, self.t["id"], "gate", "")
                for a in self.r.store.by_state(self.agent.name, ("starting", "running", "unknown", "ready", "waiting")):
                    self.r.store.update(a, state="completed")
                self.attempt(state)
                with patch.object(self.r, "_run_hook", side_effect=self.hook):
                    self.r.release_stale_gates()
                self.assertEqual(self.hooks, [])
                self.assertEqual(json.loads(self.gate_mark())["RES_ID"], "res-stale")

    def test_a_ready_attempt_with_a_live_process_keeps_its_gate(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.attempt("ready", pid=live.pid, stamp=proc_stamp(live.pid))
        with patch.object(self.r, "_run_hook", side_effect=self.hook):
            self.r.release_stale_gates()
        self.assertEqual(self.hooks, [])
        self.assertEqual(json.loads(self.gate_mark())["RES_ID"], "res-stale")

    def test_the_tick_releases_stranded_gates_before_it_resumes_anything(self):
        self.attempt("ready")
        order = []
        with patch.object(self.r, "_run_hook", side_effect=self.hook), \
                patch.object(self.r, "release_stale_gates", wraps=self.r.release_stale_gates) as sweep, \
                patch.object(self.r, "resume_ready", side_effect=lambda room: order.append("resume") or room):
            sweep.side_effect = lambda: order.append("sweep")
            self.r.tick()
        self.assertEqual(order[:2], ["sweep", "resume"])

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fake_chat_mesh import FakeChatMesh
from mesh_runner.chat_pool import ChatPool
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.chat_service import ChatService


class Echo:
    def run(self, turn, cancelled):
        yield {"type": "text_delta", "text": "re: " + turn["message"]["body"]}
        yield {"type": "final", "stop_reason": "end_turn"}


class Hold:
    def run(self, turn, cancelled):
        for _ in range(300):
            if cancelled():
                yield {"type": "final", "stop_reason": "cancelled"}
                return
            time.sleep(0.01)
        yield {"type": "final", "stop_reason": "end_turn"}


class Service(unittest.TestCase):
    def setUp(self):
        self.mesh = FakeChatMesh()
        self.client = ChatClient("https://mesh.test", "secret", opener=self.mesh)
        self.t = [1000.0]
        self.pool = ChatPool(max_per_agent=1, max_host=5, idle_minutes=10, clock=lambda: self.t[0])
        self.stopped, self.deleted = [], []
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = ChatService(self.client, ["agent-1"], self.pool, lambda a: Echo(),
                               lambda a, t: self.stopped.append((a, t)),
                               lambda a, t: self.deleted.append((a, t)) or True,
                               token_path=lambda turn: self.tmp / "tok", spawn=lambda fn: fn())

    def test_command_for_an_agent_this_runner_does_not_serve_is_not_found(self):
        self.mesh.commands["c1"] = {"command_id": "c1", "kind": "delete_transcript", "agent_id": "other",
                                    "thread_id": "thread-9"}
        self.mesh.queue_turn("x")
        self.svc.step(1)
        self.assertEqual(self.mesh.acks["c1"]["result"], "not_found")
        self.assertEqual(self.deleted, [])

    def test_claim_that_returns_a_turn_logs_its_wait_and_an_idle_claim_does_not(self):
        with self.assertLogs("mesh_runner", level="INFO") as idle:
            self.svc.step(1)
            logging.getLogger("mesh_runner").info("sentinel")
        self.assertEqual([r.getMessage() for r in idle.records], ["sentinel"])
        self.mesh.queue_turn("x")
        with self.assertLogs("mesh_runner", level="INFO") as got:
            self.svc.step(1)
        line = [r.getMessage() for r in got.records if r.getMessage().startswith("chat claim: returned in ")]
        self.assertEqual(len(line), 1)
        self.assertRegex(line[0], r"returned in \d+\.\ds, 1 turn\(s\), 0 command\(s\)")

    def test_pool_refusal_holds_claiming_instead_of_spinning(self):
        pool = ChatPool(max_per_agent=1, max_host=0, idle_minutes=10, clock=lambda: self.t[0])
        svc = ChatService(self.client, ["agent-1"], pool, lambda a: Echo(), lambda a, t: None,
                          lambda a, t: True, spawn=lambda fn: fn())
        self.mesh.queue_turn("x")
        svc.step(1)
        self.assertGreater(svc.hold_until, 0)
        before = [p for _, p in self.mesh.requests if p.startswith("/claim")]
        svc.step(1)
        self.assertEqual(before, [p for _, p in self.mesh.requests if p.startswith("/claim")])

    def test_pool_refusal_is_logged_with_its_reason(self):
        pool = ChatPool(max_per_agent=1, max_host=0, idle_minutes=10, clock=lambda: self.t[0])
        svc = ChatService(self.client, ["agent-1"], pool, lambda a: Echo(), lambda a, t: None,
                          lambda a, t: True, spawn=lambda fn: fn())
        self.mesh.queue_turn("x")
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            svc.step(1)
        self.assertTrue(any("not admitted: host_limit" in m for m in logs.output), logs.output)

    def test_unfinished_turn_is_logged_with_its_outcome(self):
        class Broken:
            def run(self, turn, cancelled):
                yield {"type": "error", "code": "cli_not_started", "message": "x", "retryable": False}
        svc = ChatService(self.client, ["agent-1"], self.pool, lambda a: Broken(), lambda a, t: None,
                          lambda a, t: True, spawn=lambda fn: fn())
        self.mesh.queue_turn("x")
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            svc.step(1)
        self.assertEqual([s for _, s in svc.outcomes], ["failed"])
        self.assertTrue(any(" failed: cli_not_started" in m for m in logs.output), logs.output)

    def test_delete_during_a_running_turn_stops_the_turn_first(self):
        self.svc.delivery_for = lambda a: Hold()
        self.svc.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        turn = self.mesh.queue_turn("slow")
        self.svc.step(1)
        time.sleep(0.2)
        self.mesh.commands["c9"] = {"command_id": "c9", "kind": "delete_transcript", "agent_id": "agent-1",
                                    "thread_id": "thread-1"}
        self.svc._command(self.mesh.commands["c9"])
        self.assertEqual(self.mesh.turns[turn["turn_id"]]["state"], "cancelled")
        self.assertFalse(self.svc.running)
        self.assertEqual(self.deleted, [("agent-1", "thread-1")])

    def test_delete_is_not_done_while_the_turn_will_not_stop(self):
        self.svc.running.add("stuck")
        self.svc.running_on["stuck"] = ("agent-1", "thread-1")
        orig = self.svc._end_turns_of
        self.svc._end_turns_of = lambda a, t, wait=0.1: orig(a, t, wait=0.1)
        self.mesh.commands["c8"] = {"command_id": "c8", "kind": "delete_transcript", "agent_id": "agent-1",
                                    "thread_id": "thread-1"}
        self.svc._command(self.mesh.commands["c8"])
        self.assertEqual(self.mesh.acks["c8"]["result"], "failed")
        self.assertEqual(self.deleted, [])

    def test_turn_runs_replies_and_session_idles_out(self):
        self.mesh.queue_turn("hello")
        self.assertEqual(self.svc.step(1), 1)
        t = next(iter(self.mesh.turns.values()))
        self.assertEqual((t["state"], t["text"]), ("done", "re: hello"))
        self.assertFalse((self.tmp / "tok").exists())          # token removed with the turn
        self.assertEqual(self.svc.reap_idle(), [])             # warm
        self.t[0] += 601
        self.assertEqual(self.svc.reap_idle(), ["thread-1"])
        self.assertEqual(self.stopped, [("agent-1", "thread-1")])

    def test_idle_reap_leaves_sessions_of_agents_served_by_another_service(self):
        self.svc.pool.admit("other", "thread-9")
        self.svc.pool.end_turn("other", "thread-9")
        self.t[0] += 601
        self.assertEqual(self.svc.reap_idle(), [])
        self.assertEqual(self.stopped, [])
        self.assertIn(("other", "thread-9"), self.svc.pool.slots)     # still held for its own service

    def test_second_chat_evicts_idle_session_of_first(self):
        self.mesh.queue_turn("a", thread="t1")
        self.svc.step(1)
        self.t[0] += 5
        self.mesh.queue_turn("b", thread="t2")
        self.svc.step(1)
        self.assertIn(("agent-1", "t1"), self.stopped)

    def test_commands_are_executed_and_acked(self):
        self.mesh.commands["c1"] = {"command_id": "c1", "kind": "delete_transcript", "agent_id": "agent-1",
                                    "thread_id": "t9"}
        self.mesh.commands["c2"] = {"command_id": "c2", "kind": "stop_session", "agent_id": "agent-1",
                                    "thread_id": "t9"}
        self.svc.step(1)
        self.assertEqual(self.deleted, [("agent-1", "t9")])
        self.assertEqual(self.mesh.acks["c1"]["result"], "done")
        self.assertEqual(self.mesh.acks["c2"]["result"], "not_found")   # nothing alive for it

    def test_orders_are_fetched_when_an_idle_claim_hides_them(self):
        self.mesh.claim_hides_commands = True
        self.mesh.commands["c1"] = {"command_id": "c1", "kind": "delete_transcript", "agent_id": "agent-1",
                                    "thread_id": "t9"}
        self.svc.step(1)
        self.assertEqual(self.deleted, [("agent-1", "t9")])
        self.assertEqual(self.mesh.acks["c1"]["result"], "done")

    def test_idle_rounds_fetch_orders_at_most_every_orders_every(self):
        calls = []
        real = self.client.commands
        self.client.commands = lambda agents: calls.append(1) or real(agents)
        self.svc.step(1)
        self.svc.step(1)
        self.assertEqual(len(calls), 1)         # the second idle round rides on the first fetch
        self.svc._next_orders = 0.0
        self.svc.step(1)
        self.assertEqual(len(calls), 2)

    def test_a_populated_claim_does_not_fetch_orders_again(self):
        calls = []
        real = self.client.commands
        self.client.commands = lambda agents: calls.append(1) or real(agents)
        self.mesh.queue_turn("x", thread="tz")
        self.mesh.commands["c1"] = {"command_id": "c1", "kind": "delete_transcript", "agent_id": "agent-1",
                                    "thread_id": "t9"}
        self.svc.step(1)
        self.assertEqual(calls, [])
        self.assertEqual(self.deleted, [("agent-1", "t9")])      # delivered with the claim answer

    def test_restart_recovery_fails_streaming_and_requeues_claimed(self):
        a = self.mesh.queue_turn("x", thread="ta")
        b = self.mesh.queue_turn("y", thread="tb")
        self.client.claim(["agent-1"], timeout=1, max_turns=2)
        ca, cb = self.mesh.turns[a["turn_id"]], self.mesh.turns[b["turn_id"]]
        self.client.start(a["turn_id"], ca["claim_token"])
        self.svc.recover()
        self.assertEqual(ca["state"], "failed")
        self.assertEqual(cb["state"], "queued")


if __name__ == "__main__":
    unittest.main()

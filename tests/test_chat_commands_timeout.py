"""A silent order fetch must not stall the claim loop: it once held it for 30 s and was
reported as "chat claim: transport after 33 s"."""
import logging
import unittest

from fake_chat_mesh import FakeChatMesh
from mesh_runner import chat_protocol as cp
from mesh_runner.chat_pool import ChatPool
from mesh_runner.chat_service import ChatService
from test_chat_claim_keepalive import FakeConn, FakeResp


class Echo:
    def run(self, turn, cancelled):
        yield {"type": "final", "stop_reason": "end_turn"}


class CommandsTimeoutTests(unittest.TestCase):
    def test_order_fetch_uses_its_short_read_timeout(self):
        mesh, seen = FakeChatMesh(), []

        def opener(req, timeout=None):
            seen.append((req.full_url, timeout))
            return mesh(req, timeout=timeout)
        cp.ChatClient("https://mesh.test", "secret", opener=opener).commands(["agent-1"])
        self.assertEqual([t for u, t in seen if "/commands" in u], [cp.COMMANDS_TIMEOUT])
        self.assertLessEqual(cp.COMMANDS_TIMEOUT, 5)

    def test_silent_order_fetch_is_logged_apart_and_does_not_fail_the_round(self):
        mesh = FakeChatMesh()

        def opener(req, timeout=None):
            if "/commands" in req.full_url:
                raise TimeoutError("The read operation timed out")
            return mesh(req, timeout=timeout)
        client = cp.ChatClient("https://mesh.test", "secret", opener=opener)
        pool = ChatPool(max_per_agent=1, max_host=5, idle_minutes=10)
        svc = ChatService(client, ["agent-1"], pool, lambda a: Echo(), lambda a, t: None, lambda a, t: True)
        with self.assertLogs("mesh_runner", level=logging.WARNING) as got:
            self.assertEqual(svc.step(1), 0)            # idle claim, then the order fetch times out
        self.assertTrue(any(r.getMessage().startswith("chat commands: transport after ") for r in got.records))
        self.assertIsNotNone(svc.claim_s)
        with self.assertLogs("mesh_runner", level=logging.WARNING):
            self.assertEqual(svc.step(1, claim_agents=[]), 0)   # no claim: orders only, still no raise
        self.assertIsNone(svc.claim_s)


class ClaimAttemptsTests(unittest.TestCase):
    def test_a_claim_lost_twice_names_both_attempts(self):
        FakeConn.made, FakeConn.script = [], [TimeoutError("timed out"), TimeoutError("timed out")]
        client = cp.ChatClient("https://mesh.example", "key")
        client._conn_factory = FakeConn
        with self.assertRaises(cp.ChatError) as cm:
            client.claim(["a"], timeout=3)
        self.assertRegex(str(cm.exception), r"\[claim attempts: \d+\.\ds, \d+\.\ds\]")

    def test_a_claim_answered_on_the_second_attempt_is_not_an_error(self):
        FakeConn.made, FakeConn.script = [], [TimeoutError("timed out"), FakeResp(204)]
        client = cp.ChatClient("https://mesh.example", "key")
        client._conn_factory = FakeConn
        self.assertIsNone(client.claim(["a"], timeout=3))


if __name__ == "__main__":
    unittest.main()

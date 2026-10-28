"""A claim that the network swallows twice must still give up within CLAIM_BUDGET_EXTRA of its
read deadline: the repeat on a fresh connection once took a second full read timeout, and a
3 s poll was reported as "chat claim: transport after 12.3s"."""
import http.client
import socket
import threading
import time
import unittest

from mesh_runner import chat_protocol as cp


class SilentServer:
    """Accepts connections on localhost and reads the request; connection number ``answer_on``
    (1-based) gets a 204, every other one is held open without a byte of reply."""

    def __init__(self, answer_on: int = 0):
        self.answer_on, self.accepted, self._held = answer_on, 0, []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._held.append(conn)
            conn.recv(65536)
            if self.accepted == self.answer_on:
                conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

    def close(self):
        self._sock.close()
        for c in self._held:
            c.close()


class ClaimBudgetTests(unittest.TestCase):
    def client(self, server):
        c = cp.ChatClient(f"https://127.0.0.1:{server.port}", "key")
        c._conn_factory = http.client.HTTPConnection        # plain HTTP to the local double
        return c

    def test_two_silent_attempts_fail_within_the_budget(self):
        server = SilentServer()
        self.addCleanup(server.close)
        t0 = time.monotonic()
        with self.assertRaises(cp.ChatError) as cm:
            self.client(server).claim(["a"], timeout=3)
        took = time.monotonic() - t0
        self.assertEqual(server.accepted, 2)                  # the repeat still happens, on a new connection
        self.assertRegex(str(cm.exception), r"\[claim attempts: \d+\.\ds, \d+\.\ds\]")
        self.assertLessEqual(took, 8.0)

    def test_silent_first_attempt_answered_second_within_the_budget(self):
        server = SilentServer(answer_on=2)
        self.addCleanup(server.close)
        t0 = time.monotonic()
        self.assertIsNone(self.client(server).claim(["a"], timeout=3))
        self.assertEqual(server.accepted, 2)
        self.assertLessEqual(time.monotonic() - t0, 8.0)


if __name__ == "__main__":
    unittest.main()

"""A silent claim timeout names the stage that stalled (connect / sent / headers)."""
import socket
import threading
import unittest
from unittest import mock

from mesh_runner import chat_protocol as cp


class ClaimPhaseTests(unittest.TestCase):
    def test_phase_note_lists_reached_stages(self):
        cp._PHASES.d = {"t0": 0.0, "connect": 0.12, "sent": 0.13}
        note = cp._phase_note()
        self.assertIn("connect=0.12s", note)
        self.assertIn("sent=0.13s", note)
        self.assertNotIn("headers", note)

    def test_phase_note_when_nothing_reached(self):
        cp._PHASES.d = {"t0": 0.0}
        self.assertIn("none reached", cp._phase_note())

    def test_timed_connection_records_send(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: srv.accept()[0].recv(1), daemon=True).start()
        cp._PHASES.d = {"t0": cp.time.monotonic()}
        conn = cp._TimedHTTPS("127.0.0.1", port, timeout=1)
        conn.sock = socket.create_connection(("127.0.0.1", port), 1)    # plain TCP stands in for TLS
        self.addCleanup(conn.sock.close)
        conn.send(b"GET / HTTP/1.1\r\n\r\n")
        self.assertIn("sent", cp._PHASES.d)

    def test_transport_timeout_carries_phases_through_client(self):
        def silent(req, timeout=None):
            cp._PHASES.d = {"t0": 0.0, "connect": 0.2, "sent": 0.2}
            raise TimeoutError("The read operation timed out")
        with mock.patch.object(cp, "_timed_open", silent):
            client = cp.ChatClient("https://mesh.example", "key")
        with self.assertRaises(cp.ChatError) as cm:
            client.active()                 # the claim loop has its own kept-alive connection
        self.assertEqual(cm.exception.error_code, "transport")
        self.assertIn("read operation timed out", str(cm.exception))
        self.assertIn("sent=0.20s", str(cm.exception))

    def test_injected_opener_error_has_no_phase_note(self):
        def silent(req, timeout=None):
            raise TimeoutError("The read operation timed out")
        client = cp.ChatClient("https://mesh.example", "key", opener=silent)
        with self.assertRaises(cp.ChatError) as cm:
            client.active()
        self.assertNotIn("phases", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

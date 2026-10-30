import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fake_chat_mesh import FakeChatMesh
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.chat_token import TokenKeeper


class Token(unittest.TestCase):
    def setUp(self):
        self.mesh = FakeChatMesh()
        self.client = ChatClient("https://mesh.test", "secret", opener=self.mesh)
        self.mesh.queue_turn()
        self.turn = self.client.claim(["agent-1"], timeout=1)["turns"][0]
        self.dir = Path(tempfile.mkdtemp())

    def test_refused_before_start_then_written_0600_and_removed(self):
        k = TokenKeeper(self.client, self.turn, self.dir / "chat" / "tok", 1, wait=lambda d: True)
        k.start()
        self.assertEqual(k.error, "chat_not_started")
        self.assertFalse((self.dir / "chat" / "tok").exists())
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        k = TokenKeeper(self.client, self.turn, self.dir / "chat" / "tok", 1, wait=lambda d: True)
        k.start()
        f = self.dir / "chat" / "tok"
        self.assertEqual(f.read_text(), "tkn-1")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        k.stop()
        self.assertFalse(f.exists())

    def test_renewal_replaces_file_and_stop_revokes_after_refusal(self):
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        ticks = iter([False, False])                 # two renewals, then the stop signal
        f = self.dir / "tok"
        k = TokenKeeper(self.client, self.turn, f, 1, wait=lambda d: next(ticks, True))
        k.start()
        k.thread.join(5)
        self.assertEqual(f.read_text(), "tkn-3")
        self.mesh.request_stop(self.turn["turn_id"])
        k2 = TokenKeeper(self.client, self.turn, f, 1, wait=lambda d: True)
        k2.start()
        self.assertEqual(k2.error, "chat_cancel_requested")

    def test_renewal_in_flight_when_stop_returns_cannot_revive_the_file(self):
        import threading
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        f = self.dir / "chat" / "tok"
        k = TokenKeeper(self.client, self.turn, f, 1, wait=lambda d: True)
        real, entered, release = self.client.session_token, threading.Event(), threading.Event()

        def slow(*a, **kw):
            res = real(*a, **kw)
            entered.set()
            release.wait(5)                          # the HTTP answer arrives after stop() returned
            return res
        self.client.session_token = slow
        t = threading.Thread(target=k.issue)
        t.start()
        self.assertTrue(entered.wait(5))
        k.stop()
        release.set()
        t.join(5)
        self.assertFalse(f.exists())
        self.assertFalse(f.parent.exists())

    def test_refused_renewal_removes_the_file(self):
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        f = self.dir / "tok"
        calls = {"n": 0}

        def wait(d):
            calls["n"] += 1
            if calls["n"] == 1:
                self.mesh.request_stop(self.turn["turn_id"])
                return False
            return True
        k = TokenKeeper(self.client, self.turn, f, 1, wait=wait)
        k.start()
        k.thread.join(5)
        self.assertEqual(k.error, "chat_cancel_requested")
        self.assertFalse(f.exists())


    def test_slow_issue_does_not_hold_the_turn(self):
        """A session-token call that hangs is waited
        for first_wait only; the file lands when the call returns."""
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        release, real = threading.Event(), self.client.session_token

        def slow(*a, **kw):
            release.wait(10)
            return real(*a, **kw)
        self.client.session_token = slow
        path = self.dir / "chat" / "tok"
        k = TokenKeeper(self.client, self.turn, path, 1, wait=lambda d: True)
        t0 = time.monotonic()
        k.start(first_wait=0.2)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(path.exists())
        release.set()
        k.thread.join(5)
        self.assertTrue(path.exists())
        k.stop()

    def test_stop_does_not_wait_for_a_hung_issue_and_the_file_never_appears(self):
        self.client.start(self.turn["turn_id"], self.turn["claim_token"])
        release, real = threading.Event(), self.client.session_token
        self.client.session_token = lambda *a, **kw: (release.wait(10), real(*a, **kw))[1]
        path = self.dir / "chat" / "tok"
        k = TokenKeeper(self.client, self.turn, path, 1, wait=lambda d: True)
        k.start(first_wait=0.1)
        t0 = time.monotonic()
        k.stop()
        self.assertLess(time.monotonic() - t0, 2.0)
        release.set()
        k.thread.join(5)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

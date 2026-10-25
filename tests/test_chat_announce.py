import threading
import unittest

from mesh_runner.chat_announce import announce, announce_loop
from mesh_runner.mesh import MeshError


class FakeMesh:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise MeshError(409, "agent is led by another runner")
        return {}


class AnnounceTests(unittest.TestCase):
    def test_announce_posts_runner_identity(self):
        m = FakeMesh()
        self.assertTrue(announce(m, "rid-1", host="h"))
        method, path, body = m.calls[0]
        self.assertEqual((method, path), ("POST", "/agents/me/controller"))
        self.assertEqual((body["runner_id"], body["name"], body["host"]), ("rid-1", "mesh-runner", "h"))
        self.assertTrue(body["version"])

    def test_conflict_is_reported_not_raised(self):
        self.assertFalse(announce(FakeMesh(fail=True), "rid-1", host="h"))

    def test_loop_announces_every_agent_until_stopped(self):
        a, b, stop = FakeMesh(), FakeMesh(), threading.Event()
        t = threading.Thread(target=announce_loop, args=({"A": a, "B": b}, "rid", stop, 0.01))
        t.start()
        while len(a.calls) < 2:
            pass
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(len(a.calls), 2)
        self.assertGreaterEqual(len(b.calls), 1)


if __name__ == "__main__":
    unittest.main()

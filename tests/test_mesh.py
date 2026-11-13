import base64
import io
import json
import unittest
import unittest.mock

from mesh_runner.mesh import MeshClient


class Opener:
    def __init__(self):
        self.req = None

    def __call__(self, req, timeout=None):
        self.req = req
        return io.BytesIO(json.dumps({"ok": True}).encode())


class BasicAuth(unittest.TestCase):
    def test_basic_auth_rides_in_authorization_beside_the_agent_key(self):
        op = Opener()
        MeshClient("https://mesh.example", "agent-key", opener=op, basic_auth="user:pa:ss").request("GET", "/x")
        headers = {k.lower(): v for k, v in op.req.header_items()}
        self.assertEqual(headers["x-agent-key"], "agent-key")
        self.assertEqual(headers["authorization"], "Basic " + base64.b64encode(b"user:pa:ss").decode())

    def test_no_basic_auth_no_authorization_header_and_repr_hides_secrets(self):
        op = Opener()
        c = MeshClient("https://mesh.example", "agent-key", opener=op)
        c.request("GET", "/x")
        self.assertNotIn("authorization", {k.lower() for k, _ in op.req.header_items()})
        self.assertNotIn("agent-key", repr(c))


class MoveSource(unittest.TestCase):
    def test_move_declares_the_runner_as_producer(self):
        op = Opener()
        MeshClient("https://mesh.example", "k", opener=op).move("t1", "s1", expected_version=3,
                                                                 expected_status_id="s0")
        self.assertTrue(op.req.full_url.endswith("/tasks/t1/move"))
        self.assertEqual(json.loads(op.req.data), {"status_id": "s1", "source": "mesh-runner",
                                                   "expected_version": 3, "expected_status_id": "s0"})


class Flaky:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise TimeoutError("timed out")
        return io.BytesIO(b'{"ok": true}')


class TransportRetry(unittest.TestCase):
    def setUp(self):
        import mesh_runner.mesh as m
        self._d, m.GET_RETRY_DELAYS = m.GET_RETRY_DELAYS, (0, 0)
        self._m = m

    def tearDown(self):
        self._m.GET_RETRY_DELAYS = self._d

    def test_get_survives_two_transport_failures(self):
        op = Flaky(2)
        self.assertEqual(MeshClient("https://m", "k", opener=op).request("GET", "/x"), {"ok": True})
        self.assertEqual(op.calls, 3)

    def test_get_gives_up_after_three_attempts(self):
        op = Flaky(9)
        with self.assertRaises(self._m.MeshError):
            MeshClient("https://m", "k", opener=op).request("GET", "/x")
        self.assertEqual(op.calls, 3)

    def test_write_is_never_repeated(self):
        op = Flaky(9)
        with self.assertRaises(self._m.MeshError):
            MeshClient("https://m", "k", opener=op).request("POST", "/x", {})
        self.assertEqual(op.calls, 1)




class RateLimited:
    def __init__(self, n, retry_after="1"):
        self.n, self.calls, self.ra = n, 0, retry_after

    def __call__(self, req, timeout=None):
        import urllib.error
        from email.message import Message
        self.calls += 1
        if self.calls <= self.n:
            h = Message()
            if self.ra is not None:
                h["Retry-After"] = self.ra
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", h,
                                         io.BytesIO(b'{"message": "Rate limit exceeded"}'))
        return io.BytesIO(b'{"ok": true}')


class RateLimitRetry(unittest.TestCase):
    def setUp(self):
        import mesh_runner.mesh as m
        self.m, self.slept = m, []
        p = unittest.mock.patch("mesh_runner.mesh.time.sleep", self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def test_two_429_then_ok_honours_retry_after_for_get_and_post(self):
        for method in ("GET", "POST"):
            op = RateLimited(2, "3")
            self.assertEqual(MeshClient("https://m", "k", opener=op).request(method, "/x", {} if method == "POST" else None),
                             {"ok": True})
            self.assertEqual(op.calls, 3)
        self.assertTrue(all(3 <= s < 4 for s in self.slept))

    def test_always_429_raises_with_endpoint_and_retry_after(self):
        op = RateLimited(99, "50")
        with self.assertRaises(self.m.MeshError) as cm:
            MeshClient("https://m", "k", opener=op).request("GET", "/tasks/abc/wake")
        self.assertEqual(op.calls, 1 + self.m.RATE_LIMIT_RETRIES)
        self.assertIn("GET /tasks/abc/wake", str(cm.exception))
        self.assertEqual(cm.exception.retry_after, 50.0)
        self.assertTrue(all(s < 30 + 6 for s in self.slept))   # capped at 30s + jitter

    def test_missing_header_uses_default(self):
        op = RateLimited(1, None)
        MeshClient("https://m", "k", opener=op).request("GET", "/x")
        self.assertGreaterEqual(self.slept[0], self.m.RATE_LIMIT_DEFAULT_SLEEP)

    def test_huge_or_infinite_retry_after_stays_capped(self):
        for ra in ("3600", "inf"):
            self.slept.clear()
            op = RateLimited(99, ra)
            with self.assertRaises(self.m.MeshError):
                MeshClient("https://m", "k", opener=op).request("GET", "/x")
            self.assertTrue(all(s <= 30 * 1.1 + 0.5 for s in self.slept), self.slept)

    def test_write_without_retry_after_is_not_repeated(self):
        op = RateLimited(1, None)
        with self.assertRaises(self.m.MeshError):
            MeshClient("https://m", "k", opener=op).request("POST", "/x", {})
        self.assertEqual(op.calls, 1)

    def test_429_does_not_spend_transport_budget(self):
        rl = RateLimited(1, "1")
        seq = []

        def opener(req, timeout=None):
            seq.append(1)
            if len(seq) == 1:
                return rl(req)
            if len(seq) <= 3:
                raise TimeoutError("t")
            return io.BytesIO(b'{"ok": true}')
        self.assertEqual(MeshClient("https://m", "k", opener=opener).request("GET", "/x"), {"ok": True})
        self.assertEqual(len(seq), 4)

    def test_http_date_retry_after_is_honoured_and_garbage_ignored(self):
        import datetime, email.utils
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=20)
        op = RateLimited(1, email.utils.format_datetime(when, usegmt=True))
        MeshClient("https://m", "k", opener=op).request("POST", "/x", {})
        self.assertTrue(10 <= self.slept[0] <= 30 * 1.1 + 0.5, self.slept)
        self.assertIsNone(self.m._parse_retry_after("soon"))
        self.assertEqual(self.m._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)


class Recording:
    """Opener that records every request; the first ``lose`` sends lose their response."""
    def __init__(self, lose=0):
        self.lose, self.reqs = lose, []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if len(self.reqs) <= self.lose:
            raise TimeoutError("timed out")          # the server may have applied it; the answer is gone
        return io.BytesIO(b'{"ok": true}')


class SessionReportIdempotencyKey(unittest.TestCase):
    def key(self, req):
        return req.get_header("Idempotency-key")

    def test_repeat_after_a_lost_response_carries_the_same_key(self):
        import mesh_runner.mesh as m
        op = Recording(lose=3)                       # the send and both same-key repeats lose their answer
        c = MeshClient("https://m", "k", opener=op)
        with unittest.mock.patch("mesh_runner.mesh.time.sleep", lambda s: None):
            with self.assertRaises(m.MeshError):
                c.session_report("t1", 100, 5, "model-a", 0.01, report_id="attempt-1:1")
            c.session_report("t1", 100, 5, "model-a", 0.01, report_id="attempt-1:1")
        self.assertEqual(len(op.reqs), 4)
        self.assertEqual(len({self.key(r) for r in op.reqs}), 1)
        k1, k2 = (self.key(r) for r in op.reqs[2:])
        self.assertTrue(k1)
        self.assertEqual(k1, k2)
        self.assertEqual(str(__import__("uuid").UUID(k1)), k1)       # a UUID
        self.assertEqual(op.reqs[0].data, op.reqs[1].data)            # the same report, byte for byte

    def test_another_report_gets_another_key(self):
        op = Recording()
        c = MeshClient("https://m", "k", opener=op)
        c.session_report("t1", 100, 5, "model-a", 0.01, report_id="attempt-1:1")
        c.session_report("t1", 100, 5, "model-a", 0.01, report_id="attempt-1:2")
        c.session_report("t1", 100, 5, "model-a", 0.01, report_id="attempt-2:1")
        keys = [self.key(r) for r in op.reqs]
        self.assertEqual(len(set(keys)), 3)

    def test_rate_limited_repeat_resends_the_same_key(self):
        slept = []
        with unittest.mock.patch("mesh_runner.mesh.time.sleep", slept.append):
            op = RateLimited(1, "0")
            seen = []
            orig = op.__call__
            c = MeshClient("https://m", "k", opener=lambda req, timeout=None: (seen.append(req), orig(req, timeout))[1])
            c.session_report("t1", 1, 1, "m", 0.0, report_id="attempt-1:1")
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.key(seen[0]), self.key(seen[1]))

    def test_without_a_report_id_no_key_is_sent(self):
        op = Recording()
        MeshClient("https://m", "k", opener=op).session_report("t1", 1, 1, "m", 0.0)
        self.assertIsNone(self.key(op.reqs[0]))


if __name__ == "__main__":
    unittest.main()

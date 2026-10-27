"""The claim loop keeps one connection and repeats a silent poll once before reporting an error."""
import http.client
import json
import unittest

from mesh_runner import chat_protocol as cp


class FakeResp:
    def __init__(self, status, body=b"", will_close=False):
        self.status, self._body, self.will_close, self.reason = status, body, will_close, "x"

    def read(self):
        return self._body


class FakeConn:
    made = []
    script = []          # one entry per request: a FakeResp, or an exception to raise

    def __init__(self, netloc, timeout=None):
        self.netloc, self.timeout, self.sock, self.closed, self.requests = netloc, timeout, None, False, []
        FakeConn.made.append(self)

    def request(self, method, path, headers=None):
        self.requests.append((method, path, headers))

    def getresponse(self):
        item = FakeConn.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class ClaimKeepAliveTests(unittest.TestCase):
    def setUp(self):
        FakeConn.made, FakeConn.script = [], []
        self.client = cp.ChatClient("https://mesh.example", "key")
        self.client._conn_factory = FakeConn

    def test_idle_polls_reuse_one_connection(self):
        FakeConn.script = [FakeResp(204), FakeResp(204), FakeResp(204)]
        for _ in range(3):
            self.assertIsNone(self.client.claim(["a"], timeout=3))
        self.assertEqual(len(FakeConn.made), 1)
        self.assertEqual(len(FakeConn.made[0].requests), 3)
        method, path, headers = FakeConn.made[0].requests[0]
        self.assertTrue(path.startswith("/api/v1/runner/chat/claim?"))
        self.assertEqual(headers["X-Agent-Key"], "key")

    def test_turns_are_returned(self):
        FakeConn.script = [FakeResp(200, json.dumps({"turns": [{"id": "t"}]}).encode())]
        self.assertEqual(self.client.claim(["a"], timeout=3)["turns"][0]["id"], "t")

    def test_silent_poll_is_repeated_once_on_a_fresh_connection(self):
        FakeConn.script = [TimeoutError("The read operation timed out"), FakeResp(204)]
        self.assertIsNone(self.client.claim(["a"], timeout=3))
        self.assertEqual(len(FakeConn.made), 2)
        self.assertTrue(FakeConn.made[0].closed)

    def test_two_silent_polls_are_a_transport_error(self):
        FakeConn.script = [TimeoutError("t1"), http.client.RemoteDisconnected("t2")]
        with self.assertRaises(cp.ChatError) as cm:
            self.client.claim(["a"], timeout=3)
        self.assertEqual(cm.exception.error_code, "transport")

    def test_http_error_is_not_repeated(self):
        body = json.dumps({"error": {"error_code": "chat_stale_claim", "message": "m"}}).encode()
        FakeConn.script = [FakeResp(409, body)]
        with self.assertRaises(cp.ChatError) as cm:
            self.client.claim(["a"], timeout=3)
        self.assertEqual(cm.exception.error_code, "chat_stale_claim")
        self.assertEqual(FakeConn.script, [])

    def test_malformed_answer_is_not_repeated(self):
        FakeConn.script = [FakeResp(200, b"<html>")]
        with self.assertRaises(cp.ChatError):
            self.client.claim(["a"], timeout=3)
        self.assertEqual(FakeConn.script, [])
        self.assertEqual(len(FakeConn.made[0].requests), 1)

    def test_server_close_closes_the_socket(self):
        # http.client reopens a closed connection by itself on the next request
        FakeConn.script = [FakeResp(204, will_close=True)]
        self.client.claim(["a"], timeout=3)
        self.assertTrue(FakeConn.made[0].closed)


if __name__ == "__main__":
    unittest.main()

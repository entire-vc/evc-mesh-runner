"""Real TLS boundary: a local TLS server records what arrives, per ``recv`` after the handshake."""
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request

from mesh_runner import coalesced_http as ch
from mesh_runner.mesh import MeshClient


def _cert(d):
    crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return crt, key


class Server:
    """Reads the request until the declared body is complete, noting each chunk's size; answers a fixed JSON."""

    def __init__(self, crt, key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.ctx, self.requests = ctx, []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return                                  # the listener was closed
            try:
                conn = self.ctx.wrap_socket(raw, server_side=True)
            except OSError:
                continue                                # a client that refused the certificate
            buf, chunks = b"", []
            while b"\r\n\r\n" not in buf:
                # TLS records are never merged by recv(): one recv returns at most one record's data
                part = conn.recv(65536)
                if not part:
                    break
                chunks.append(len(part))
                buf += part
            head, _, body = buf.partition(b"\r\n\r\n")
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1])
            while len(body) < n:
                part = conn.recv(65536)
                if not part:
                    break
                chunks.append(len(part))
                body += part
            self.requests.append({"head": head, "body": body, "chunks": chunks})
            out = b'{"ok":true}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % len(out) + out)
            conn.close()

    def close(self):
        self.sock.close()


class CoalescedWrite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.crt, cls.key = _cert(cls.tmp.name)
        cls.srv = Server(cls.crt, cls.key)
        cls.ctx = ssl.create_default_context(cafile=cls.crt)
        cls.url = f"https://localhost:{cls.srv.port}/p"

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    def _send(self, req, coalesce=True):
        self.srv.requests.clear()
        h = ch.CoalescingHTTPSHandler(context=self.ctx) if coalesce else urllib.request.HTTPSHandler(context=self.ctx)
        with urllib.request.build_opener(h).open(req, timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        return self.srv.requests[-1]

    def _post(self, body, method="POST"):
        return urllib.request.Request(self.url, data=body, method=method, headers={"Content-Type": "application/json"})

    def test_control_stock_path_splits_headers_and_body(self):
        # positive control: the observation distinguishes the two paths
        got = self._send(self._post(b'{"a":1}'), coalesce=False)
        self.assertEqual(len(got["chunks"]), 2)

    def test_small_bytes_body_is_one_record_and_unchanged(self):
        body = json.dumps({"state": "x" * 480}).encode()
        for method in ("POST", "PUT"):
            got = self._send(self._post(body, method))
            self.assertEqual(got["chunks"], [len(got["head"]) + 4 + len(body)])
            self.assertEqual(got["body"], body)
            self.assertTrue(got["head"].startswith(method.encode() + b" /p HTTP/1.1\r\n"))
            self.assertIn(b"Content-Length: %d" % len(body), got["head"])
            self.assertIn(b"Host: localhost:%d" % self.srv.port, got["head"])

    def test_get_takes_the_stock_path(self):
        got = self._send(urllib.request.Request(self.url))
        self.assertEqual(got["body"], b"")
        self.assertTrue(got["head"].startswith(b"GET /p HTTP/1.1\r\n"))

    def test_oversize_body_takes_the_stock_path(self):
        body = b"y" * (ch.COALESCE_MAX + 1)
        got = self._send(self._post(body))
        self.assertEqual(got["body"], body)
        self.assertGreaterEqual(len(got["chunks"]), 2)
        self.assertEqual(got["chunks"][0], len(got["head"]) + 4)      # the header block went alone

    def test_stream_body_takes_the_stock_path_chunked(self):
        import io
        req = urllib.request.Request(self.url, data=io.BytesIO(b"stream-body"), method="POST",
                                     headers={"Content-Length": "11"})
        got = self._send(req)
        self.assertEqual(got["body"], b"stream-body")
        self.assertEqual(got["chunks"][0], len(got["head"]) + 4)

    def test_empty_body_takes_the_stock_path(self):
        got = self._send(self._post(b""))
        self.assertEqual(got["body"], b"")

    def test_certificate_is_still_verified(self):
        other = ssl.create_default_context()        # does not trust the test CA
        opener = urllib.request.build_opener(ch.CoalescingHTTPSHandler(context=other))
        with self.assertRaises(OSError):
            opener.open(self._post(b"{}"), timeout=10)

    def test_redirect_policy_of_the_caller_is_kept(self):
        class No(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = ch.build_opener(No())
        self.assertTrue(any(isinstance(h, No) for h in op.handlers))
        self.assertFalse(any(type(h) is urllib.request.HTTPSHandler for h in op.handlers))
        self.assertTrue(any(isinstance(h, ch.CoalescingHTTPSHandler) for h in op.handlers))

    def test_mesh_client_default_path_sends_one_record(self):
        self.srv.requests.clear()
        op = ch.build_opener()
        op.handlers  # default opener also honours proxy env: unchanged handler set otherwise
        c = MeshClient(f"https://localhost:{self.srv.port}", "k",
                       opener=lambda req, timeout=None: urllib.request.build_opener(
                           ch.CoalescingHTTPSHandler(context=self.ctx)).open(req, timeout=timeout))
        self.assertEqual(c.request("PUT", "/tasks/t/runtime-state", {"kind": "working"}), {"ok": True})
        got = self.srv.requests[-1]
        self.assertEqual(len(got["chunks"]), 1)
        self.assertEqual(json.loads(got["body"]), {"kind": "working"})

    def test_default_client_uses_coalescing_urlopen(self):
        self.assertEqual(MeshClient("https://m", "k")._open, ch.urlopen)


if __name__ == "__main__":
    unittest.main()

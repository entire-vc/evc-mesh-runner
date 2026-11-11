"""Explicit main-Mesh route through the real Runner transport: TLS server, CONNECT proxies, real files.

The TLS server holds a certificate for ``mesh.entire.host`` from a test CA trusted through ``SSL_CERT_FILE``;
CONNECT proxies on loopback forward every tunnel to it, so nothing here resolves or reaches the real host.
``HOME`` points at a temporary directory, so the activation path is the production one under it.
"""
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from mesh_runner import chat_protocol, coalesced_http as ch, main_route
from mesh_runner.mesh import MeshClient, MeshError

MAIN = "https://mesh.entire.host"
KEY = "agent-key-under-test"


def _cert(d):
    crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=mesh.entire.host",
                    "-addext", "subjectAltName=DNS:mesh.entire.host,DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return crt, key


def _read_head(conn):
    buf = b""
    while b"\r\n\r\n" not in buf:
        part = conn.recv(65536)
        if not part:
            break
        buf += part
    return buf.partition(b"\r\n\r\n")


class TLSServer:
    """Records SNI, head and body per request; ``/to-local`` and ``/to-main`` answer with a redirect."""

    def __init__(self, crt, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(crt, key)
        self.ctx.sni_callback = self._sni
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.requests, self._names = [], threading.local()
        threading.Thread(target=self._accept, daemon=True).start()

    def _sni(self, sslsock, name, ctx):
        sslsock.sni = name

    def _accept(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except OSError:
            raw.close()
            return                                      # a client that refused the certificate
        head, _, body = _read_head(conn)
        n = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                n = int(line.split(b":")[1])
        while len(body) < n:
            part = conn.recv(65536)
            if not part:
                break
            body += part
        path = head.split(b" ")[1].decode()
        self.requests.append({"sni": getattr(conn, "sni", None), "head": head, "body": body, "path": path})
        if path.endswith("/to-local"):
            status, extra = b"302 Found", b"Location: https://localhost:%d/landed\r\n" % self.port
        elif path.endswith("/to-main"):
            status, extra = b"302 Found", b"Location: https://mesh.entire.host/landed\r\n"
        else:
            status, extra = b"200 OK", b""
        out = b'{"ok":true}'
        conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n" + extra
                     + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(out) + out)
        conn.close()

    def close(self):
        self.sock.close()


class ConnectProxy:
    """A loopback CONNECT proxy: records each CONNECT head and every byte the client tunnels, forwards to target."""

    def __init__(self, target_port):
        self.target = target_port
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.connects, self.tunnelled = [], bytearray()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        head, _, rest = _read_head(c)
        self.connects.append(head)
        if not head.startswith(b"CONNECT "):
            c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return c.close()
        up = socket.create_connection(("127.0.0.1", self.target))
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            self.tunnelled += rest
            up.sendall(rest)

        def pump(src, dst, record):
            try:
                while data := src.recv(65536):
                    if record:
                        self.tunnelled += data
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        back = threading.Thread(target=pump, args=(up, c, False), daemon=True)
        back.start()
        pump(c, up, True)
        back.join(10)
        c.close()
        up.close()

    def close(self):
        self.sock.close()


class MainRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.crt, cls.key = _cert(cls.tmp.name)
        cls.srv = TLSServer(cls.crt, cls.key)
        cls.route = ConnectProxy(cls.srv.port)          # the loopback main route
        cls.envproxy = ConnectProxy(cls.srv.port)       # an ordinary environment proxy (the old path)
        cls.local = f"https://localhost:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.route, cls.envproxy):
            s.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        (Path(self.home.name) / ".config").mkdir()
        self.activation = Path(self.home.name) / ".config" / "mesh-main-route.json"
        for s in (self.srv, self.route, self.envproxy):
            getattr(s, "requests", getattr(s, "connects", None)).clear()
        self.route.tunnelled.clear()
        self.env()
        ch._default = None                              # the default opener reads proxies when built
        self.addCleanup(setattr, ch, "_default", None)

    def env(self, **extra):
        base = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        base.update({"HOME": self.home.name, "SSL_CERT_FILE": self.crt})
        base.update(extra)
        p = mock.patch.dict(os.environ, base, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def activate(self, enabled=True, **over):
        doc = {"version": 1, "enabled": enabled, "proxy_url": self.route.url, **over}
        self.activation.write_text(json.dumps(doc))

    def client(self):
        return MeshClient(MAIN, KEY, timeout=10)

    # -- routed ----------------------------------------------------------------------------------------
    def test_main_opt_in_end_to_end_tls_body_over_4k(self):
        self.env(NO_PROXY="mesh.entire.host", no_proxy="mesh.entire.host")    # the route overrides NO_PROXY
        self.activate()
        body = {"blob": "x" * 6000}
        self.assertEqual(self.client().request("POST", "/tasks/t/comments", body), {"ok": True})
        self.assertEqual(len(self.route.connects), 1)
        connect = self.route.connects[0]
        self.assertTrue(connect.startswith(b"CONNECT mesh.entire.host:443 HTTP/1.1\r\n"), connect)
        self.assertNotIn(b"proxy-authorization", connect.lower())
        got = self.srv.requests[-1]
        self.assertEqual(got["sni"], "mesh.entire.host")                    # original SNI, end to end
        self.assertGreater(len(got["body"]), 4096)
        self.assertEqual(json.loads(got["body"]), body)
        self.assertIn(b"Host: mesh.entire.host\r\n", got["head"])
        self.assertIn(b"X-Agent-Key: " + KEY.encode(), got["head"])         # credentials unchanged
        self.assertNotIn(KEY.encode(), bytes(self.route.tunnelled))          # the proxy saw only TLS
        self.assertEqual(self.envproxy.connects, [])

    def test_routed_certificate_is_still_verified(self):
        self.env(SSL_CERT_FILE=os.path.join(self.tmp.name, "absent.pem"))  # the test CA is not trusted
        self.activate()
        with self.assertRaises(MeshError) as e:
            self.client().request("POST", "/x", {"a": 1})
        self.assertIn("CERTIFICATE_VERIFY_FAILED", e.exception.args[0] if e.exception.args else str(e.exception))
        self.assertEqual(len(self.route.connects), 1)
        self.assertEqual(self.srv.requests, [])

    def test_caller_proxyhandler_empty_still_routes_main_only(self):
        self.env(HTTPS_PROXY=self.envproxy.url, https_proxy=self.envproxy.url)
        self.activate()
        op = ch.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPRedirectHandler)
        with op.open(urllib.request.Request(MAIN + "/a", data=b"{}", method="POST"), timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        with op.open(urllib.request.Request(self.local + "/b", data=b"{}", method="POST"), timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        self.assertEqual(len(self.route.connects), 1)                       # main through the route
        self.assertEqual(self.envproxy.connects, [])                        # ProxyHandler({}) still honoured
        self.assertEqual([r["sni"] for r in self.srv.requests], ["mesh.entire.host", "localhost"])

    def test_foreign_proxy_authorization_is_dropped(self):
        self.activate()
        req = urllib.request.Request(MAIN + "/a", data=b"{}", method="POST",
                                     headers={"Proxy-Authorization": "Basic Zm9yZWlnbjpwcm94eQ=="})
        with ch.build_opener().open(req, timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        self.assertNotIn(b"proxy-authorization", self.route.connects[0].lower())
        self.assertNotIn(b"proxy-authorization", self.srv.requests[-1]["head"].lower())

    def test_redirect_scope_follows_the_target_origin(self):
        self.activate()
        op = ch.build_opener()
        with op.open(MAIN + "/to-local", timeout=10) as r:                   # main -> other origin: direct
            r.read()
        self.assertEqual(len(self.route.connects), 1)
        self.assertEqual([(q["path"], q["sni"]) for q in self.srv.requests],
                         [("/to-local", "mesh.entire.host"), ("/landed", "localhost")])
        self.srv.requests.clear()
        with op.open(self.local + "/to-main", timeout=10) as r:              # other origin -> main: routed
            r.read()
        self.assertEqual(len(self.route.connects), 2)
        self.assertEqual([(q["path"], q["sni"]) for q in self.srv.requests],
                         [("/to-main", "localhost"), ("/landed", "mesh.entire.host")])

    def test_chat_protocol_open_routes_main(self):
        self.activate()
        with chat_protocol._timed_open(urllib.request.Request(MAIN + "/c", data=b"{}", method="POST"),
                                       timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        self.assertEqual(len(self.route.connects), 1)

    # -- not activated: the old path -------------------------------------------------------------------
    def test_missing_or_disabled_activation_keeps_the_old_path(self):
        self.env(HTTPS_PROXY=self.envproxy.url, https_proxy=self.envproxy.url)
        for setup in (lambda: None, lambda: self.activate(enabled=False)):
            self.envproxy.connects.clear()
            ch._default = None
            setup()
            self.assertEqual(self.client().request("POST", "/x", {"a": "y" * 5000}), {"ok": True})
            self.assertEqual(len(self.envproxy.connects), 1)                # the environment proxy, as before
            self.assertEqual(self.route.connects, [])

    def test_no_activation_honours_no_proxy_as_before(self):
        self.env(HTTPS_PROXY=self.envproxy.url, https_proxy=self.envproxy.url,
                 NO_PROXY="mesh.entire.host", no_proxy="mesh.entire.host")
        req = urllib.request.Request(MAIN + "/x", data=b"{}", method="POST")
        h = main_route.MainRouteProxyHandler()
        self.assertIsNone(h.https_open(req))
        self.assertFalse(req.has_proxy())                                   # direct, the stock bypass decision
        stock = urllib.request.Request(MAIN + "/x", data=b"{}", method="POST")
        urllib.request.ProxyHandler().https_open(stock)
        self.assertEqual(req.has_proxy(), stock.has_proxy())

    # -- fail closed / scope ---------------------------------------------------------------------------
    def test_invalid_enabled_activation_fails_closed_for_main(self):
        self.env(HTTPS_PROXY=self.envproxy.url, https_proxy=self.envproxy.url)
        bad = [b"{", b"[]", json.dumps({"version": 2, "enabled": True, "proxy_url": self.route.url}).encode(),
               json.dumps({"version": True, "enabled": True, "proxy_url": self.route.url}).encode(),
               json.dumps({"version": 1, "enabled": "yes", "proxy_url": self.route.url}).encode()]
        for url in ("https://127.0.0.1:1", "http://localhost:1", "http://127.0.0.1", "http://u:p@127.0.0.1:1",
                    "http://127.0.0.1:1/x", "http://127.0.0.1:99999", "http://[::1]:1", 7):
            bad.append(json.dumps({"version": 1, "enabled": True, "proxy_url": url}).encode())
        bad.append(json.dumps({"version": 1, "enabled": True, "proxy_url": self.route.url,
                               "pad": "p" * 4096}).encode())
        for raw in bad:
            ch._default = None
            self.activation.write_bytes(raw)
            with self.assertRaises(MeshError, msg=raw[:60]) as e:
                self.client().request("POST", "/x", {"a": 1})
            self.assertEqual(e.exception.status, 0)
            self.assertIn("main route activation invalid", str(e.exception))
        self.activation.unlink()
        self.activation.mkdir()                                             # unreadable as a file
        with self.assertRaises(MeshError):
            self.client().request("POST", "/x", {"a": 1})
        self.assertEqual(self.route.connects, [])
        self.assertEqual(self.envproxy.connects, [])                        # no fallback to the old path
        self.assertEqual(self.srv.requests, [])

    def test_other_origins_never_read_the_activation(self):
        self.activation.write_bytes(b"{ not json")                          # would fail any main request
        other = MeshClient(self.local, KEY, timeout=10)
        self.assertEqual(other.request("POST", "/x", {"a": 1}), {"ok": True})
        self.assertEqual(self.route.connects, [])
        self.assertEqual(self.srv.requests[-1]["sni"], "localhost")
        for url in ("https://other.mesh.entire.host/api", "https://mesh.entire.host:8443/", "http://mesh.entire.host/",
                    "https://mesh.entire.host.example/", "https://u:p@mesh.entire.host/", "https://mesh.entire.host:x/",
                    self.local):
            self.assertFalse(main_route.is_main(url), url)
        for url in ("https://mesh.entire.host/api/v1", "https://MESH.entire.host:443/x"):
            self.assertTrue(main_route.is_main(url), url)

    def test_with_route_keeps_a_caller_proxy_subclass(self):
        class Own(urllib.request.ProxyHandler):
            pass
        own = Own({})
        self.assertEqual(main_route.with_route([own]), [own])
        self.assertEqual(main_route.with_route([Own]), [Own])
        hs = main_route.with_route([urllib.request.ProxyHandler({"https": "http://127.0.0.1:9"})])
        self.assertIs(type(hs[0]), main_route.MainRouteProxyHandler)
        self.assertEqual(hs[0].original_proxies, {"https": "http://127.0.0.1:9"})
        op = ch.build_opener()
        self.assertEqual(sum(isinstance(h, urllib.request.ProxyHandler) for h in op.handlers), 1)


if __name__ == "__main__":
    unittest.main()

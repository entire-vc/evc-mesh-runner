"""One TLS record for the headers and a small bytes body of an HTTPS request.

``http.client`` sends the header block and the body with two ``send`` calls. On some direct egress paths the
separate body segment after the TLS handshake is sometimes lost (in 600 POSTs per variant the separate
form hung 13 times, headers+body in one write 0 times). For a bounded ``bytes`` body both parts go out in one write.

Nothing else changes: same bytes on the wire, TLS/SNI/certificate checks, proxy handling, redirect policy,
timeouts and retries stay with the caller. File objects, iterables, chunked bodies, GET and bodies above
``COALESCE_MAX`` take the stock ``http.client`` path.
"""
from __future__ import annotations

import http.client
import urllib.request

COALESCE_MAX = 64 * 1024        # the memory bound of the extra copy


class CoalesceSendMixin:
    """For an ``http.client.HTTPSConnection`` subclass: ``headers + body`` in a single ``send``."""

    def _send_output(self, message_body=None, encode_chunked=False):
        if (type(message_body) is not bytes or not message_body or encode_chunked
                or len(message_body) > COALESCE_MAX or self._method == "GET"):
            return super()._send_output(message_body, encode_chunked)
        self._buffer.extend((b"", b""))
        msg = b"\r\n".join(self._buffer)
        del self._buffer[:]
        self.send(msg + message_body)


class CoalescingHTTPSConnection(CoalesceSendMixin, http.client.HTTPSConnection):
    pass


class CoalescingHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(CoalescingHTTPSConnection, req, context=self._context)


def build_opener(*handlers) -> urllib.request.OpenerDirector:
    """``urllib.request.build_opener`` with the coalescing HTTPS handler in place of the stock one."""
    return urllib.request.build_opener(*handlers, CoalescingHTTPSHandler())


_default = None


def urlopen(req, timeout=None):
    """Drop-in for ``urllib.request.urlopen(req, timeout=...)`` (default handlers, env proxies, redirects)."""
    global _default
    if _default is None:
        _default = build_opener()
    if timeout is None:
        return _default.open(req)
    return _default.open(req, timeout=timeout)

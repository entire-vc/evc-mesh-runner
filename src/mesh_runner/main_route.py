"""Explicit opt-in route for main Mesh HTTPS through a loopback CONNECT proxy.

The route is switched on by a nonsecret activation file, separate from every credential carrier:
``~/.config/mesh-main-route.json``, at most 4096 bytes,
``{"version": 1, "enabled": true, "proxy_url": "http://127.0.0.1:<port>"}``.

- Only ``https://mesh.entire.host`` on port 443 is routed. Every other origin (a partner instance, GitLab, a localhost
  test server) is decided before the file is read and keeps the caller's proxy policy unchanged.
- File missing or ``enabled: false``: the existing transport, exactly as before.
- File present but invalid: a main request fails closed (``URLError``, nothing is sent), never falls back
  to the direct path.
- Routed: a ``CONNECT mesh.entire.host:443`` tunnel through the loopback proxy. TLS, SNI and certificate
  verification stay end-to-end with the original host; the route overrides ``NO_PROXY`` and a caller's
  ``ProxyHandler({})`` for this origin only, and drops a ``Proxy-Authorization`` meant for another proxy.
  No retries, no redirect policy and no credential handling are added here.

The file is read on every main request, so turning the route on or off needs no Runner restart.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MAIN_HOST = "mesh.entire.host"
ACTIVATION_MAX = 4096


def activation_path() -> Path:
    return Path.home() / ".config" / "mesh-main-route.json"


def is_main(url: str) -> bool:
    try:
        origin = urlsplit(url)
        port = origin.port
    except ValueError:
        return False
    return (origin.scheme == "https" and origin.hostname == MAIN_HOST and port in (None, 443)
            and origin.username is None and origin.password is None)


def proxy_authority(path: Path | None = None) -> str | None:
    """``127.0.0.1:<port>`` when the route is enabled, None when absent or disabled; raises when invalid."""
    try:
        with (path or activation_path()).open("rb") as source:
            raw = source.read(ACTIVATION_MAX + 1)          # bounded: never a large or secret-bearing file
    except FileNotFoundError:
        return None
    if len(raw) > ACTIVATION_MAX:
        raise ValueError("main route activation too large")
    config = json.loads(raw)
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("invalid main route activation version")
    enabled = config.get("enabled")
    if type(enabled) is not bool:
        raise ValueError("invalid main route activation state")
    if not enabled:
        return None
    url = config.get("proxy_url")
    if not isinstance(url, str):
        raise ValueError("invalid main route proxy URL")
    proxy = urlsplit(url)
    if (proxy.scheme != "http" or proxy.hostname != "127.0.0.1" or proxy.port is None
            or not 1 <= proxy.port <= 65535 or proxy.username is not None or proxy.password is not None
            or proxy.path not in ("", "/") or proxy.query or proxy.fragment):
        raise ValueError("main route proxy must be plain IPv4 loopback")
    return f"127.0.0.1:{proxy.port}"


class MainRouteProxyHandler(urllib.request.ProxyHandler):
    """A ``ProxyHandler`` with the caller's proxies (None: the environment's) plus the main route."""

    def __init__(self, proxies=None, activation: Path | None = None):
        self.activation = activation
        self.original_proxies = dict(urllib.request.getproxies() if proxies is None else proxies)
        # an https_open must exist even when the caller disabled proxies, so main requests reach proxy_open
        super().__init__({**self.original_proxies, "https": self.original_proxies.get("https", "")})

    def proxy_open(self, req, proxy, type):
        if type == "https" and is_main(req.full_url):
            try:
                authority = proxy_authority(self.activation)
            except (OSError, ValueError, TypeError) as e:
                raise urllib.error.URLError(f"main route activation invalid, request not sent: "
                                            f"{type_name(e)}") from None
            if authority:
                req.remove_header("Proxy-authorization")
                req.set_proxy(authority, "http")
                return None
        if proxy:
            return super().proxy_open(req, proxy, type)
        return None


def type_name(e: BaseException) -> str:
    return f"{e.__class__.__name__}: {str(e)[:120]}"


def with_route(handlers) -> list:
    """The caller's handlers with a plain ``ProxyHandler`` (class or instance) turned into the routed one.

    Without any proxy handler the routed one takes the environment's proxies, as urllib's default does.
    A ``ProxyHandler`` subclass of the caller is kept as is: its own policy wins.
    """
    out, seen = [], False
    for h in handlers:
        if h is urllib.request.ProxyHandler:
            out.append(MainRouteProxyHandler())
            seen = True
        elif type(h) is urllib.request.ProxyHandler:
            out.append(MainRouteProxyHandler(h.proxies))
            seen = True
        else:
            if isinstance(h, urllib.request.ProxyHandler) or (
                    isinstance(h, type) and issubclass(h, urllib.request.ProxyHandler)):
                seen = True
            out.append(h)
    if not seen:
        out.insert(0, MainRouteProxyHandler())
    return out

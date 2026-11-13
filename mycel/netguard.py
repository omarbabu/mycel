"""Network exposure rules, enforced in code rather than left to configuration.

The node runs two listeners:

* **control** (dashboard, local OpenAI-compatible API, limit/location settings) — always
  loopback. There is no option to change this. Reach it remotely via an SSH port-forward.
* **mesh** (peer protocol) — loopback by default. A non-loopback bind requires BOTH a
  specific interface address (wildcards like 0.0.0.0 / :: are refused outright) AND an
  explicit ``allow_non_loopback = true``. The intended non-loopback target is the address
  of a private overlay interface (WireGuard / Tailscale / Headscale), never a public NIC.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from aiohttp import web

WILDCARDS = {"0.0.0.0", "::", "", "*"}


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_mesh_bind(host: str, allow_non_loopback: bool) -> str:
    if host in WILDCARDS:
        raise ValueError(
            f"refusing wildcard mesh bind {host!r}; bind a specific overlay interface address instead"
        )
    if is_loopback_host(host):
        return host
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"mesh bind host must be an IP address, got {host!r}") from exc
    if not allow_non_loopback:
        raise ValueError(
            f"mesh bind {host} is not loopback; set mesh.allow_non_loopback = true only for a "
            "private overlay interface address (see README 'Going multi-machine')"
        )
    return host


_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]"}


def _strip_port(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[: host_header.find("]") + 1]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


@web.middleware
async def control_plane_guard(request: web.Request, handler):
    """Defend the loopback control plane against browser-borne attacks.

    * Host header must be a loopback name — blocks DNS-rebinding from malicious websites.
    * Cross-origin browser requests are refused (no CORS is ever granted).
    * State-changing requests must be JSON — HTML forms can't send that cross-site without
      a CORS preflight, which we never approve.
    """
    if _strip_port(request.headers.get("Host", "")) not in _LOCAL_HOSTNAMES:
        raise web.HTTPForbidden(text="control plane is local-only")
    origin = request.headers.get("Origin")
    if origin is not None:
        o = urlparse(origin)
        if not (o.hostname and is_loopback_host(o.hostname) and o.port == request.url.port):
            raise web.HTTPForbidden(text="cross-origin request refused")
    if request.method in ("POST", "PUT", "PATCH"):  # DELETE can't come from a form and is preflighted cross-site
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON body required")
    if request.method == "OPTIONS":
        raise web.HTTPForbidden(text="CORS not supported")
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
    )
    return resp

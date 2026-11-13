"""``mycel relay``: a small, stateless forwarder that lets nodes behind NAT reach each other.

Home machines open **no inbound ports**. Each node keeps an *outbound* WebSocket to two or
three relays; a relay forwards sealed frames by node id and never sees plaintext (see
``rpc.py`` — every payload is end-to-end encrypted before it reaches the relay).

Admission: a node proves it holds the key behind its node id (signed challenge) and presents
its proof-of-work identity ticket, so flooding a relay costs real work per identity. Each
connection gets a byte and frame rate limit, a bounded send queue and a maximum frame size;
over-limit frames are dropped with a ``nack`` to the sender, never buffered without bound.

Relays also act as a rendezvous: a freshly joined node asks for a random sample of connected
node ids and starts gossiping with them. Anyone can run a relay; it holds no state on disk.

Public operation (see ``deploy/relay``): the relay runs behind a TLS-terminating load balancer
(``--behind-load-balancer``), which is then the only thing that can reach it. Unauthenticated
handshakes are capped globally and per source address, ``/health`` turns 503 while draining so
the balancer stops sending new connections, and SIGTERM closes connections with "going away" so
nodes move to another relay. The relay logs counts only: never node ids, IPs or payloads.

Frame layout (binary WebSocket messages, same in both directions)::

    kind:u8 | id_len:u8 | node id (utf-8) | stream id: 8 bytes | payload

Outbound the id is the recipient; inbound it is the authenticated sender.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from .identity import b64d, node_id_for
from .netguard import validate_mesh_bind
from . import trust

log = logging.getLogger("mycel.relay")

REQ, RES, END, ERR, HELLO, HELLO_RES, CANCEL = range(1, 8)
KINDS = {REQ, RES, END, ERR, HELLO, HELLO_RES, CANCEL}
AUTH_TAG = b"mycel-relay-auth-v1|"
PROTO = 1


def pack(kind: int, node_id: str, sid: bytes, payload: bytes) -> bytes:
    nid = node_id.encode()
    return struct.pack("BB", kind, len(nid)) + nid + sid + payload


def unpack(frame: bytes) -> tuple[int, str, bytes, bytes]:
    if len(frame) < 10:
        raise ValueError("short frame")
    kind, n = frame[0], frame[1]
    if kind not in KINDS or len(frame) < 2 + n + 8:
        raise ValueError("bad frame")
    return kind, frame[2:2 + n].decode(), frame[2 + n:10 + n], frame[10 + n:]


def auth_message(nonce: str, relay_id: str) -> bytes:
    return AUTH_TAG + nonce.encode() + b"|" + relay_id.encode()


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, time.monotonic()

    def take(self, n: float) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens < n:
            return False
        self.tokens -= n
        return True


@dataclass
class RelayLimits:
    max_frame: int = 2 << 20             # bytes per frame
    bytes_per_s: float = 8 << 20          # sustained per node
    burst_bytes: float = 32 << 20
    frames_per_s: float = 400.0
    burst_frames: float = 1200.0
    queue_frames: int = 512               # per-recipient send queue
    max_connections: int = 20_000
    max_pending: int = 256                # unauthenticated handshakes in flight (whole relay)
    per_ip_pending: int = 16              # ... from one source address
    per_ip_connections: int = 512         # authenticated connections from one address (CGNAT shares IPs)
    handshakes_per_ip_s: float = 2.0      # sustained new handshakes per address
    handshake_burst: float = 40.0
    auth_timeout_s: float = 10.0


@dataclass
class _Conn:
    node_id: str
    ws: web.WebSocketResponse
    queue: asyncio.Queue
    bytes_bucket: TokenBucket
    frame_bucket: TokenBucket
    since: float = field(default_factory=time.monotonic)


class Relay:
    def __init__(self, relay_id: str | None = None, region: str = "", limits: RelayLimits | None = None,
                 audit: bool = False, trust_proxy: bool = False):
        self.relay_id = relay_id or "relay-" + secrets.token_hex(4)
        self.region = region
        self.limits = limits or RelayLimits()
        self.trust_proxy = trust_proxy   # take the client address from X-Forwarded-For (only behind our own LB)
        self.conns: dict[str, _Conn] = {}
        self.pending = 0
        self.pending_by_ip: dict[str, int] = {}
        self.conns_by_ip: dict[str, int] = {}
        self.hs_buckets: dict[str, TokenBucket] = {}
        self.draining = False
        self.started = time.time()
        self.stats = {"frames": 0, "bytes": 0, "nacks": 0, "rate_limited": 0, "auth_failed": 0,
                      "connections_total": 0, "congested": 0, "handshakes_refused": 0}
        # Test-only: the last payloads that passed through, to prove they're ciphertext.
        self.audit_log: deque[bytes] | None = deque(maxlen=4000) if audit else None
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    # ------------------------------------------------------------------ lifecycle
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/health", self._health)
        app.router.add_get("/status", self._status)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 0, allow_public: bool = False,
                    behind_load_balancer: bool = False) -> int:
        validate_relay_bind(host, allow_public, behind_load_balancer)
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        self.port = self._runner.addresses[-1][1]
        return self.port

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def drain(self) -> None:
        """Stop taking connections and ask every node to move ("going away")."""
        self.draining = True
        for c in list(self.conns.values()):
            with contextlib.suppress(Exception):
                await c.ws.send_json({"t": "draining"})
                await c.ws.close(code=1001, message=b"relay restarting")

    async def stop(self) -> None:
        for c in list(self.conns.values()):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, _r):
        """Load-balancer health check: 503 while draining so no new connections land here."""
        return web.json_response({"ok": not self.draining}, status=503 if self.draining else 200)

    async def _status(self, _r):
        """Public counters for a status page. No node ids, no addresses."""
        from . import __version__
        return web.json_response({"relay_id": self.relay_id, "region": self.region, "version": __version__,
                                  "proto": PROTO, "uptime_s": round(time.time() - self.started),
                                  "draining": self.draining, "connected": len(self.conns), **self.stats})

    def client_ip(self, request: web.Request) -> str:
        if self.trust_proxy:
            # The balancer appends the real client address last; anything before it is client-supplied.
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.rsplit(",", 1)[-1].strip()[:64]
        return (request.remote or "?")[:64]

    def _admit_handshake(self, ip: str) -> bool:
        L = self.limits
        if self.draining or self.pending >= L.max_pending or self.pending_by_ip.get(ip, 0) >= L.per_ip_pending:
            return False
        if self.conns_by_ip.get(ip, 0) >= L.per_ip_connections:
            return False
        bucket = self.hs_buckets.get(ip)
        if bucket is None:
            if len(self.hs_buckets) > 50_000:  # bounded memory under address-spraying
                now = time.monotonic()
                self.hs_buckets = {k: b for k, b in self.hs_buckets.items() if now - b.t < 60}
            bucket = self.hs_buckets[ip] = TokenBucket(L.handshakes_per_ip_s, L.handshake_burst)
        return bucket.take(1)

    # ------------------------------------------------------------------ connections
    async def _authenticate(self, ws: web.WebSocketResponse) -> str | None:
        nonce = secrets.token_hex(16)
        await ws.send_json({"t": "challenge", "nonce": nonce, "relay_id": self.relay_id,
                            "region": self.region, "max_frame": self.limits.max_frame, "proto": PROTO})
        try:
            msg = await asyncio.wait_for(ws.receive(), self.limits.auth_timeout_s)
            data = json.loads(msg.data) if msg.type == WSMsgType.TEXT else None
            if not isinstance(data, dict) or data.get("t") != "auth":
                raise ValueError("expected auth")
            node, pub = str(data["node"]), str(data["pubkey"])
            pub_raw = b64d(pub)
            if node_id_for(pub_raw) != node:
                raise ValueError("node id does not match key")
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(pub_raw).verify(b64d(data["sig"]), auth_message(nonce, self.relay_id))
            trust.verify_ticket(data.get("ticket"), node, pub)   # proof of work per identity
            return node
        except Exception as exc:  # any failure: refuse without detail
            self.stats["auth_failed"] += 1
            log.debug("relay auth failed: %s", exc)
            return None

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        ip = self.client_ip(request)
        if len(self.conns) >= self.limits.max_connections or not self._admit_handshake(ip):
            self.stats["handshakes_refused"] += 1
            return web.json_response({"t": "error", "why": "relay busy"}, status=503)  # before any upgrade
        self.pending += 1
        self.pending_by_ip[ip] = self.pending_by_ip.get(ip, 0) + 1
        ws = web.WebSocketResponse(max_msg_size=self.limits.max_frame + 512, heartbeat=20)
        try:
            await ws.prepare(request)
            node = await self._authenticate(ws)
        finally:
            self.pending -= 1
            left = self.pending_by_ip[ip] - 1
            if left:
                self.pending_by_ip[ip] = left
            else:
                del self.pending_by_ip[ip]
        if node is None:
            with contextlib.suppress(Exception):   # the client may already be gone
                await ws.send_json({"t": "error", "why": "authentication failed"})
                await ws.close()
            return ws
        self.conns_by_ip[ip] = self.conns_by_ip.get(ip, 0) + 1
        try:
            return await self._session(ws, node)
        finally:
            left = self.conns_by_ip[ip] - 1
            if left:
                self.conns_by_ip[ip] = left
            else:
                del self.conns_by_ip[ip]

    async def _session(self, ws: web.WebSocketResponse, node: str) -> web.WebSocketResponse:
        L = self.limits
        conn = _Conn(node, ws, asyncio.Queue(L.queue_frames), TokenBucket(L.bytes_per_s, L.burst_bytes),
                     TokenBucket(L.frames_per_s, L.burst_frames))
        old = self.conns.get(node)
        self.conns[node] = conn       # newest connection for a node wins
        if old is not None:
            await old.ws.close()
        self.stats["connections_total"] += 1
        writer = asyncio.create_task(self._writer(conn))
        await ws.send_json({"t": "welcome", "node": node, "connected": len(self.conns)})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self._forward(conn, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await self._control(conn, msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            writer.cancel()
            if self.conns.get(node) is conn:
                del self.conns[node]
        return ws

    async def _writer(self, conn: _Conn) -> None:
        try:
            while True:
                item = await conn.queue.get()
                if isinstance(item, (bytes, bytearray)):
                    await conn.ws.send_bytes(item)
                else:
                    await conn.ws.send_json(item)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass

    def _enqueue(self, conn: _Conn, item) -> bool:
        try:
            conn.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    def _nack(self, conn: _Conn, to: str, sid: bytes, why: str) -> None:
        self.stats["nacks"] += 1
        self._enqueue(conn, {"t": "nack", "to": to, "s": sid.hex(), "why": why})

    async def _forward(self, src: _Conn, frame: bytes) -> None:
        try:
            kind, to, sid, payload = unpack(frame)
        except (ValueError, UnicodeDecodeError):
            return
        if not (src.frame_bucket.take(1) and src.bytes_bucket.take(len(frame))):
            self.stats["rate_limited"] += 1
            return self._nack(src, to, sid, "rate limited")
        dst = self.conns.get(to)
        if dst is None:
            return self._nack(src, to, sid, "offline")
        if not self._enqueue(dst, pack(kind, src.node_id, sid, payload)):
            self.stats["congested"] += 1
            return self._nack(src, to, sid, "congested")
        self.stats["frames"] += 1
        self.stats["bytes"] += len(payload)
        if self.audit_log is not None and payload:
            self.audit_log.append(bytes(payload))

    async def _control(self, conn: _Conn, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict) or not conn.frame_bucket.take(1):
            return
        if data.get("t") == "peers":
            n = max(1, min(32, int(data.get("n", 16)) if str(data.get("n", "16")).isdigit() else 16))
            others = [k for k in self.conns if k != conn.node_id]
            self._enqueue(conn, {"t": "peers", "nodes": random.sample(others, min(n, len(others)))})


async def serve(host: str, port: int, region: str, allow_public: bool, behind_load_balancer: bool = False,
                relay_id: str | None = None, drain_s: float = 5.0) -> None:
    import signal
    relay = Relay(relay_id=relay_id, region=region, trust_proxy=behind_load_balancer)
    await relay.start(host, port, allow_public, behind_load_balancer)
    mode = " behind a load balancer" if behind_load_balancer else ""
    print(f"mycel relay {relay.relay_id} listening on ws://{host}:{relay.port}/ws{mode} (region {region or '-'})", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stats = asyncio.create_task(_log_stats(relay))
    try:
        await stop.wait()
        log.warning("draining")
        relay.draining = True          # health check goes 503 first, so the balancer stops routing here
        await asyncio.sleep(drain_s)
        await relay.drain()
    finally:
        stats.cancel()
        await relay.stop()


async def _log_stats(relay: Relay) -> None:
    """One line a minute of counters (what operators need for dashboards; nothing identifying)."""
    while True:
        await asyncio.sleep(60)
        log.warning("stats %s", json.dumps({"connected": len(relay.conns), "pending": relay.pending, **relay.stats}))


def validate_relay_bind(host: str, allow_public: bool, behind_load_balancer: bool) -> str:
    """Relays bind loopback unless the operator says otherwise. ``behind_load_balancer`` is for a
    container whose only ingress is the operator's TLS load balancer (security group from the LB
    only, no public IP); there a wildcard bind on the task's private interface is the norm."""
    if behind_load_balancer and host in ("0.0.0.0", "::"):
        return host
    return validate_mesh_bind(host, allow_public)

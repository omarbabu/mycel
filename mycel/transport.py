"""How sealed messages travel between nodes. Everything above this layer is transport-agnostic.

* ``HTTPTransport`` — direct node-to-node HTTP on a mesh listener (loopback for single-machine
  meshes, or a private overlay address). Requires the peer to accept inbound connections.
* ``RelayTransport`` — the default for real networks. The node opens **outbound** WebSockets
  to a few relays and needs no inbound port at all, so it works behind any NAT.

Both carry the same opaque payloads: an HPKE-sealed request, answered by a stream of sealed
chunks (see ``rpc.py``). Neither ever sees plaintext.

``Netem`` injects WAN conditions (latency, jitter, retransmit stalls, loss) for local
simulations, so routing and split inference can be tested realistically on one machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import random
import socket
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Callable
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from aiohttp.resolver import ThreadedResolver

from .identity import b64e
from .netguard import is_loopback_host
from .relay import CANCEL, END, ERR, HELLO, HELLO_RES, REQ, RES, auth_message, pack, unpack

if TYPE_CHECKING:
    from .node import Node

log = logging.getLogger("mycel")
MAX_SERVING_PER_PEER = 32


class PeerUnreachable(Exception):
    pass


# ---------------------------------------------------------------------------- address safety
def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified or ip.is_reserved)


def relay_url_ok(url: str, loopback_only: bool) -> bool:
    """May this node connect to a relay URL taken from config or a peer's advert?

    A loopback-only node connects only to loopback relays (simulations never leave the
    machine). Otherwise relays must use TLS (``wss://``) and must not point into private,
    loopback or link-local address space — a malicious advert can't aim us at local services.
    """
    if not isinstance(url, str) or len(url) > 200:
        return False
    try:
        u = urlparse(url)
        host, port = u.hostname, u.port
    except ValueError:
        return False
    if u.scheme not in ("ws", "wss") or not host or u.username or u.password:
        return False
    if loopback_only:
        return is_loopback_host(host)
    if u.scheme != "wss":
        return False
    try:
        return _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        return "." in host  # a DNS name; resolved addresses are checked at connect time


class PublicOnlyResolver(ThreadedResolver):
    """DNS resolver that refuses non-public answers (blocks DNS-rebinding into local networks)."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        out = [r for r in await super().resolve(host, port, family)
               if _ip_is_public(ipaddress.ip_address(r["host"]))]
        if not out:
            raise OSError(f"{host} resolves only to non-public addresses")
        return out


# ---------------------------------------------------------------------------- WAN emulation
@dataclass
class Netem:
    """One-way delay ``latency(src, dst)`` seconds, plus jitter, stalls and loss (simulation only)."""
    latency: Callable[[str, str], float]
    jitter: float = 0.1        # +/- fraction of the delay
    stall_p: float = 0.0       # probability a frame suffers a TCP-retransmit-like stall
    stall_s: float = 0.2
    drop_p: float = 0.0        # probability a frame is lost outright (tests failure handling)

    def delay(self, src: str, dst: str) -> float | None:
        if self.drop_p and random.random() < self.drop_p:
            return None
        d = max(0.0, self.latency(src, dst))
        d *= 1 + random.uniform(-self.jitter, self.jitter)
        if self.stall_p and random.random() < self.stall_p:
            d += self.stall_s
        return d


# ---------------------------------------------------------------------------- direct HTTP
class HTTPTransport:
    name = "direct"

    def __init__(self, node: "Node"):
        self.node = node
        self.session: aiohttp.ClientSession | None = None

    def routes(self, app: web.Application) -> None:
        app.router.add_post("/mesh/hello", self._h_hello)
        app.router.add_post("/mesh/rpc", self._h_rpc)

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(connect=5, sock_read=90))

    async def stop(self) -> None:
        if self.session:
            await self.session.close()

    def can_reach(self, hint: dict) -> bool:
        addr = hint.get("addr")
        return isinstance(addr, str) and self.node._dial_ok(addr)

    async def _h_hello(self, _r):
        return web.json_response(self.node.contact_card())

    async def _h_rpc(self, request: web.Request) -> web.StreamResponse:
        blob = await request.read()
        resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        try:
            gen = self.node.serve_rpc(blob, via="direct")
            first = await anext(gen, None)
        except Exception as exc:  # undecryptable or malformed: the node decides what's safe to say
            return web.json_response({"error": str(exc)[:200]}, status=400)
        await resp.prepare(request)
        try:
            if first is not None:
                await resp.write(struct.pack(">I", len(first)) + first)
                async for chunk in gen:
                    await resp.write(struct.pack(">I", len(chunk)) + chunk)
            await resp.write_eof()
        except ConnectionResetError:
            await gen.aclose()
        return resp

    async def hello(self, node_id: str, hint: dict) -> dict:
        if not self.can_reach(hint):
            raise PeerUnreachable("address not dialable")
        try:
            async with self.session.post(f"http://{hint['addr']}/mesh/hello", json={}) as r:
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise PeerUnreachable(type(exc).__name__) from exc

    async def call(self, node_id: str, hint: dict, blob: bytes, timeout: float = 90) -> AsyncIterator[bytes]:
        if not self.can_reach(hint):
            raise PeerUnreachable("address not dialable")
        try:
            async with self.session.post(f"http://{hint['addr']}/mesh/rpc", data=blob,
                                         headers={"Content-Type": "application/octet-stream"}) as r:
                if r.status != 200:
                    try:
                        msg = (await r.json()).get("error", "")
                    except (ValueError, aiohttp.ContentTypeError):
                        msg = ""
                    raise PeerUnreachable(f"HTTP {r.status} {msg}".strip())
                while True:
                    try:
                        head = await r.content.readexactly(4)
                    except asyncio.IncompleteReadError:
                        return
                    (n,) = struct.unpack(">I", head)
                    yield await r.content.readexactly(n)
        except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise PeerUnreachable(f"transport error: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------- relays
class _RelayConn:
    def __init__(self, url: str):
        self.url = url
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.relay_id = ""
        self.region = ""
        self.out: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.last_used = time.monotonic()
        self.peers_future: asyncio.Future | None = None
        self.sids: set[bytes] = set()

    @property
    def up(self) -> bool:
        return self.ready.is_set() and self.ws is not None and not self.ws.closed


class RelayTransport:
    """Outbound-only connectivity through relays (works behind NAT; opens no ports)."""
    name = "relay"

    def __init__(self, node: "Node", urls: list[str], netem: Netem | None = None, max_visits: int = 8, book=None):
        self.node = node
        self.book = book   # RelayBook in internet mode: remembers relays we reached, supplies fallbacks
        self.urls = [u for u in dict.fromkeys(urls)]
        self.netem = netem
        self.max_visits = max_visits
        self.home: dict[str, _RelayConn] = {}
        self.visits: dict[str, _RelayConn] = {}
        self.pending: dict[bytes, asyncio.Queue] = {}
        self.serving: dict[tuple[str, bytes], asyncio.Task] = {}
        self.session: aiohttp.ClientSession | None = None
        self._maint: list[asyncio.Task] = []
        self._maint_by_url: dict[str, asyncio.Task] = {}
        self._next_at: dict[str, float] = {}
        self.stats = {"sent": 0, "received": 0, "reconnects": 0}

    # ------------------------------------------------------------------ lifecycle
    def url_ok(self, url: str) -> bool:
        return relay_url_ok(url, self.node.loopback_mesh)

    async def start(self) -> None:
        connector = None if self.node.loopback_mesh else aiohttp.TCPConnector(resolver=PublicOnlyResolver())
        self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(connect=8))
        for url in self.urls:
            if self.url_ok(url):
                conn = _RelayConn(url)
                self.home[url] = conn
                self._spawn_maintain(conn)
            else:
                log.warning("ignoring relay %s (not allowed for this node)", url)
        self._maint.append(asyncio.create_task(self._heal_loop()))
        if self.home:  # wait briefly for the first connection so the first advert is reachable
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.wait([asyncio.ensure_future(c.ready.wait()) for c in self.home.values()],
                                                    return_when=asyncio.FIRST_COMPLETED), 5)

    async def stop(self) -> None:
        for t in self._maint:
            t.cancel()
        for t in list(self.serving.values()):
            t.cancel()
        for c in [*self.home.values(), *self.visits.values()]:
            for t in c.tasks:
                t.cancel()
            if c.ws is not None:
                await c.ws.close()
        await asyncio.gather(*self._maint, return_exceptions=True)
        if self.session:
            await self.session.close()

    def _spawn_maintain(self, conn: _RelayConn) -> None:
        t = asyncio.create_task(self._maintain(conn))
        self._maint.append(t)
        self._maint_by_url[conn.url] = t

    def add_home(self, url: str) -> None:
        """Start keeping a connection to another relay (e.g. one the user just added)."""
        if url in self.home or not self.url_ok(url):
            return
        conn = self.visits.pop(url, None) or _RelayConn(url)
        self.home[url] = conn
        self._spawn_maintain(conn)

    async def remove_home(self, url: str) -> None:
        conn = self.home.pop(url, None)
        if conn is None:
            return
        t = self._maint_by_url.pop(url, None)
        if t is not None:
            t.cancel()
        for t in conn.tasks:
            t.cancel()
        if conn.ws is not None:
            await conn.ws.close()

    def home_urls(self) -> list[str]:
        return [u for u, c in self.home.items() if c.up]

    def connected_relays(self) -> list[dict]:
        return [{"url": c.url, "relay_id": c.relay_id, "region": c.region, "up": c.up} for c in self.home.values()]

    async def _maintain(self, conn: _RelayConn) -> None:
        backoff = 0.5
        while conn.url in self.home:
            try:
                await self._connect(conn)
                backoff = 0.5
                await self._wait_closed(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("relay %s: %s", conn.url, exc)
            conn.ready.clear()
            self._fail_pending(conn, "relay connection lost")
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff * random.uniform(0.8, 1.2))
            backoff = min(30.0, backoff * 2)

    async def _heal_loop(self) -> None:
        """If every home relay is down for a while, adopt another relay learned from peers' adverts
        (or the configured list) — a node is never stranded because its relay died."""
        down_since = None
        while True:
            await asyncio.sleep(2.0)
            if any(c.up for c in self.home.values()):
                down_since = None
                continue
            down_since = down_since or time.monotonic()
            if time.monotonic() - down_since < 6.0:
                continue
            known = [u for p in self.node.membership.peers.values() for u in (p.body.get("relays") or [])]
            remembered = self.book.fallbacks() if self.book else []
            fresh = [u for u in dict.fromkeys(known + self.urls + remembered) if u not in self.home and self.url_ok(u)]
            if fresh:
                url = random.choice(fresh)
                conn = self.visits.pop(url, None) or _RelayConn(url)
                self.home[url] = conn
                self._spawn_maintain(conn)
                log.info("all relays down; adopted %s", url)
                down_since = None

    async def _wait_closed(self, conn: _RelayConn) -> None:
        reader, writer = conn.tasks
        await asyncio.gather(reader, return_exceptions=True)   # the reader ends when the socket closes
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    async def _connect(self, conn: _RelayConn) -> None:
        ticket = await self.node.ensure_ticket()
        ws = await self.session.ws_connect(conn.url, heartbeat=20, max_msg_size=8 << 20)
        hello = await ws.receive_json(timeout=10)
        if hello.get("t") != "challenge":
            raise PeerUnreachable("relay did not send a challenge")
        ident = self.node.identity
        sig = ident.private_key.sign(auth_message(hello["nonce"], hello["relay_id"]))
        await ws.send_json({"t": "auth", "node": ident.node_id, "pubkey": ident.pubkey, "sig": b64e(sig), "ticket": ticket})
        welcome = await ws.receive_json(timeout=10)
        if welcome.get("t") != "welcome":
            raise PeerUnreachable(f"relay refused: {welcome.get('why', 'unknown')}")
        conn.ws, conn.relay_id, conn.region = ws, str(hello.get("relay_id", ""))[:64], str(hello.get("region", ""))[:32]
        conn.out = asyncio.Queue()
        conn.tasks = [asyncio.create_task(self._reader(conn)), asyncio.create_task(self._writer(conn))]
        conn.ready.set()
        if self.book is not None:
            self.book.note_connected(conn.url)

    async def _writer(self, conn: _RelayConn) -> None:
        while True:
            item = await conn.out.get()
            if isinstance(item, (bytes, bytearray)):
                await conn.ws.send_bytes(item)
            else:
                await conn.ws.send_json(item)

    def _send(self, conn: _RelayConn, kind: int, to: str, sid: bytes, payload: bytes) -> None:
        frame = pack(kind, to, sid, payload)
        conn.last_used = time.monotonic()
        self.stats["sent"] += 1
        if self.netem is None:
            conn.out.put_nowait(frame)
            return
        d = self.netem.delay(self.node.id, to)
        if d is None:
            return  # lost
        loop = asyncio.get_running_loop()
        at = max(loop.time() + d, self._next_at.get(to, 0.0) + 1e-6)  # per-destination FIFO, like TCP
        self._next_at[to] = at
        loop.call_at(at, conn.out.put_nowait, frame)

    async def _reader(self, conn: _RelayConn) -> None:
        async for msg in conn.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                try:
                    kind, peer, sid, payload = unpack(msg.data)
                except (ValueError, UnicodeDecodeError):
                    continue
                self.stats["received"] += 1
                self._on_frame(conn, kind, peer, sid, payload)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("t") == "nack":
                    q = self.pending.get(bytes.fromhex(data.get("s", "")) if _is_hex(data.get("s")) else b"")
                    if q:
                        q.put_nowait((ERR, f"relay: peer {data.get('why', 'unreachable')}".encode()))
                elif data.get("t") == "peers" and conn.peers_future and not conn.peers_future.done():
                    conn.peers_future.set_result([n for n in data.get("nodes", []) if isinstance(n, str)][:32])
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    def _on_frame(self, conn: _RelayConn, kind: int, peer: str, sid: bytes, payload: bytes) -> None:
        if kind in (RES, END, ERR, HELLO_RES):
            q = self.pending.get(sid)
            if q is not None:
                q.put_nowait((kind, payload))
        elif kind == REQ:
            if sum(1 for (p, _) in self.serving if p == peer) >= MAX_SERVING_PER_PEER:
                self._send(conn, ERR, peer, sid, b"too many concurrent requests")
                return
            key = (peer, sid)
            self.serving[key] = asyncio.create_task(self._serve(conn, peer, sid, payload))
        elif kind == CANCEL:
            t = self.serving.pop((peer, sid), None)
            if t:
                t.cancel()
        elif kind == HELLO:
            self._send(conn, HELLO_RES, peer, sid, json.dumps(self.node.contact_card()).encode())

    async def _serve(self, conn: _RelayConn, peer: str, sid: bytes, blob: bytes) -> None:
        try:
            async for chunk in self.node.serve_rpc(blob, via="relay"):
                self._send(conn, RES, peer, sid, chunk)
            self._send(conn, END, peer, sid, b"")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._send(conn, ERR, peer, sid, str(exc)[:200].encode())
        finally:
            self.serving.pop((peer, sid), None)

    def _fail_pending(self, conn: _RelayConn, why: str) -> None:
        for sid in list(conn.sids):
            q = self.pending.get(sid)
            if q:
                q.put_nowait((ERR, why.encode()))
        conn.sids.clear()

    # ------------------------------------------------------------------ client side
    def can_reach(self, hint: dict) -> bool:
        relays = hint.get("relays")
        return isinstance(relays, list) and any(isinstance(u, str) and self.url_ok(u) for u in relays[:4])

    async def _conn_for(self, hint: dict) -> _RelayConn:
        relays = [u for u in (hint.get("relays") or [])[:4] if isinstance(u, str) and self.url_ok(u)]
        for u in relays:  # a relay we're already on
            c = self.home.get(u) or self.visits.get(u)
            if c is not None and c.up:
                return c
        for u in relays:  # visit one of the peer's relays (outbound, like any other)
            if u in self.home:
                continue
            if len(self.visits) >= self.max_visits:
                oldest = min(self.visits.values(), key=lambda c: c.last_used)
                self.visits.pop(oldest.url)
                for t in oldest.tasks:
                    t.cancel()
                if oldest.ws is not None:
                    await oldest.ws.close()
            conn = _RelayConn(u)
            try:
                await asyncio.wait_for(self._connect(conn), 10)
            except (Exception, asyncio.TimeoutError) as exc:
                log.debug("visit relay %s failed: %s", u, exc)
                continue
            self.visits[u] = conn
            return conn
        raise PeerUnreachable("none of the peer's relays are reachable")

    async def hello(self, node_id: str, hint: dict) -> dict:
        conn = await self._conn_for(hint)
        sid = os.urandom(8)
        q: asyncio.Queue = asyncio.Queue()
        self.pending[sid] = q
        conn.sids.add(sid)
        try:
            self._send(conn, HELLO, node_id, sid, b"")
            kind, payload = await asyncio.wait_for(q.get(), 15)
            if kind != HELLO_RES:
                raise PeerUnreachable(payload.decode(errors="replace") or "no answer")
            return json.loads(payload)
        except (asyncio.TimeoutError, ValueError) as exc:
            raise PeerUnreachable("hello timed out") from exc
        finally:
            self.pending.pop(sid, None)
            conn.sids.discard(sid)

    async def call(self, node_id: str, hint: dict, blob: bytes, timeout: float = 90) -> AsyncIterator[bytes]:
        conn = await self._conn_for(hint)
        sid = os.urandom(8)
        q: asyncio.Queue = asyncio.Queue()
        self.pending[sid] = q
        conn.sids.add(sid)
        finished = False
        try:
            self._send(conn, REQ, node_id, sid, blob)
            while True:
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout)
                except asyncio.TimeoutError as exc:
                    raise PeerUnreachable("timed out waiting for the peer") from exc
                if kind == RES:
                    yield payload
                elif kind == END:
                    finished = True
                    return
                else:
                    finished = True
                    raise PeerUnreachable(payload.decode(errors="replace") or "peer error")
        finally:
            self.pending.pop(sid, None)
            conn.sids.discard(sid)
            if not finished and conn.up:
                self._send(conn, CANCEL, node_id, sid, b"")

    async def rendezvous(self, n: int = 16) -> list[tuple[str, dict]]:
        """Ask our relays for a random sample of connected nodes (bootstrap for a fresh node)."""
        out: list[tuple[str, dict]] = []
        for c in list(self.home.values()):
            if not c.up:
                continue
            c.peers_future = asyncio.get_running_loop().create_future()
            c.out.put_nowait({"t": "peers", "n": n})
            try:
                nodes = await asyncio.wait_for(c.peers_future, 5)
            except asyncio.TimeoutError:
                continue
            out += [(nid, {"relays": [c.url]}) for nid in nodes if nid != self.node.id]
        return out


def _is_hex(s) -> bool:
    try:
        bytes.fromhex(s)
        return isinstance(s, str)
    except (ValueError, TypeError):
        return False

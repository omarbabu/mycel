"""Internet mode: relay selection and memory, consent before contacting relays, and a relay that
holds up when it's public (handshake caps, draining, nothing identifying in its status)."""
import asyncio
import json
import tempfile
import time
from pathlib import Path

import aiohttp
import pytest

import mycel.node as node_mod
from mycel.config import BackendConfig, MeshConfig, NodeConfig
from mycel.identity import Identity
from mycel.node import Node
from mycel.relay import Relay, RelayLimits, validate_relay_bind
from mycel.relays import RelayBook
from mycel.transport import relay_url_ok
from mycel.trust import mint_ticket

from test_transport_privacy import HW, _cfg, _ws_auth

PUBLIC = "wss://relay-a.mycel.invalid/ws"


def run(c):
    return asyncio.run(c)


def test_internet_relay_urls_must_be_public_tls():
    ok = lambda u: relay_url_ok(u, loopback_only=False)
    assert ok(PUBLIC) and ok("wss://203.0.113.9/ws") is False  # TEST-NET is reserved, not public
    assert ok("wss://8.8.8.8/ws")
    for bad in ("ws://relay.example.org/ws", "wss://127.0.0.1/ws", "wss://10.0.0.5/ws", "wss://192.168.1.2/ws",
                "wss://169.254.169.254/ws", "wss://user:pw@relay.example.org/ws", "https://relay.example.org", "wss://localhost/ws"):
        assert not ok(bad), bad
    with pytest.raises(ValueError):
        NodeConfig(mesh=MeshConfig(internet=True, listen=True)).validate()   # internet nodes open no port


def test_relay_book_priorities_and_memory(monkeypatch):
    d = Path(tempfile.mkdtemp())
    ok = lambda u: relay_url_ok(u, False)
    seeds = [f"wss://seed{i}.mycel.invalid/ws" for i in range(5)]
    monkeypatch.setattr("mycel.relays.bundled_relays", lambda: seeds)
    book = RelayBook(d, ok, configured=["wss://cfg.mycel.invalid/ws"])
    book.add_user("wss://mine.mycel.invalid/ws")
    with pytest.raises(ValueError):
        book.add_user("ws://insecure.mycel.invalid/ws")
    home = book.home_set()
    assert home[:2] == ["wss://mine.mycel.invalid/ws", "wss://cfg.mycel.invalid/ws"] and len(home) == 3
    assert home[2] in seeds
    book.note_connected("wss://learned.mycel.invalid/ws")
    book.note_connected("wss://10.0.0.1/ws")                     # never remembers a non-public relay
    book.set_use_bundled(False)
    again = RelayBook(d, ok)                                   # survives a restart
    assert again.user == ["wss://mine.mycel.invalid/ws"] and not again.use_bundled
    assert list(again.learned) == ["wss://learned.mycel.invalid/ws"]
    assert again.home_set() == ["wss://mine.mycel.invalid/ws", "wss://learned.mycel.invalid/ws"]
    # Stale learned relays are forgotten.
    raw = json.loads((d / "relays.json").read_text())
    raw["learned"]["wss://learned.mycel.invalid/ws"] = time.time() - 40 * 86400
    (d / "relays.json").write_text(json.dumps(raw))
    assert not RelayBook(d, ok).learned


def test_internet_node_contacts_relays_only_after_joining(monkeypatch):
    started = []

    class FakeRelay:
        def __init__(self, node, urls, netem=None, book=None):
            self.urls, self.home = urls, {}
        async def start(self):
            started.append(list(self.urls))
        async def stop(self):
            started.append("stopped")
        def connected_relays(self):
            return [{"url": u, "relay_id": "r", "region": "", "up": False} for u in self.urls]
        def home_urls(self):
            return []
        def add_home(self, url):
            self.urls.append(url)
        async def remove_home(self, url):
            self.urls.remove(url)

    monkeypatch.setattr(node_mod, "RelayTransport", FakeRelay)

    async def main():
        cfg = NodeConfig(name="n", data_dir=tempfile.mkdtemp(), control_port=0, probe_hardware=False, simulated_hardware=HW,
                         mesh=MeshConfig(listen=False, internet=True, relays=[PUBLIC]), backend=BackendConfig(kind="mock", models=["m"]))
        n = Node(cfg, Identity.generate())
        await n.start()
        try:
            assert n.http is None and n.relay is None and not started      # no port, no relay contact before joining
            view = n.network_view()
            assert view["mode"] == "internet" and not view["online"] and view["known"][0]["source"] == "config"
            await n.join_global()
            assert started == [[PUBLIC]]
            await n.add_relay("wss://mine.mycel.invalid/ws")
            assert n.relay.urls == [PUBLIC, "wss://mine.mycel.invalid/ws"]
            with pytest.raises(ValueError):
                await n.add_relay("wss://127.0.0.1:9/ws")
            n.leave_mesh()
            await asyncio.sleep(0.05)
            assert n.relay is None and started[-1] == "stopped"
        finally:
            await n.stop()
    run(main())


def test_runtime_add_and_remove_relay():
    async def main():
        r1, r2 = Relay(), Relay()
        await r1.start(), await r2.start()
        n = Node(_cfg("x", r1.url), Identity.generate())
        await n.join_global()
        await n.start()
        try:
            n.relay.add_home(r2.url)
            for _ in range(50):
                if len(n.relay.home_urls()) == 2:
                    break
                await asyncio.sleep(0.05)
            assert set(n.relay.home_urls()) == {r1.url, r2.url} and n.id in r2.conns
            await n.relay.remove_home(r2.url)
            await asyncio.sleep(0.2)
            assert n.relay.home_urls() == [r1.url] and n.id not in r2.conns
        finally:
            await n.stop()
            await r1.stop(), await r2.stop()
    run(main())


def test_public_relay_caps_handshakes_drains_and_reveals_nothing():
    async def main():
        relay = Relay(limits=RelayLimits(per_ip_pending=2, handshake_burst=3, handshakes_per_ip_s=0.01))
        await relay.start()
        sessions = []
        try:
            # Idle unauthenticated sockets: the third concurrent one from the same address is refused before upgrade.
            for _ in range(2):
                s = aiohttp.ClientSession()
                sessions.append((s, await s.ws_connect(relay.url)))   # keep the socket open (idle, never authenticates)
            async with aiohttp.ClientSession() as s:
                with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                    await s.ws_connect(relay.url)
                assert e.value.status == 503
            for s, _ in sessions:
                await s.close()
            await asyncio.sleep(0.1)
            # Handshake rate per address: burst of 3 used up (2 above + this one), then refused.
            good = Identity.generate()
            s, ws, msg = await _ws_auth(relay.url, good, mint_ticket(good))
            assert msg["t"] == "welcome"
            async with aiohttp.ClientSession() as s2:
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await s2.ws_connect(relay.url)
            base = relay.url.replace("ws://", "http://").removesuffix("/ws")
            async with aiohttp.ClientSession() as h:
                st = await (await h.get(base + "/status")).json()
                assert st["connected"] == 1 and good.node_id not in json.dumps(st) and "127.0.0.1" not in json.dumps(st)
                assert (await h.get(base + "/health")).status == 200
                await relay.drain()
                assert (await h.get(base + "/health")).status == 503
            got = await ws.receive_json(timeout=2)
            assert got["t"] == "draining"
            closed = await ws.receive(timeout=2)
            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 1001   # "going away": move to another relay
            await s.close()
        finally:
            await relay.stop()
    run(main())


def test_client_address_only_from_our_balancer():
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/ws", headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
    assert Relay(trust_proxy=True).client_ip(req) == "9.9.9.9"      # the hop our LB appended, not the spoofable ones
    assert Relay(trust_proxy=False).client_ip(req) != "1.2.3.4"
    assert validate_relay_bind("0.0.0.0", False, True) == "0.0.0.0"
    with pytest.raises(ValueError):
        validate_relay_bind("0.0.0.0", True, False)

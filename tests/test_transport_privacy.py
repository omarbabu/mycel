"""E2E sealing, relay admission/limits, and what a node reveals by default."""
import asyncio
import tempfile

import aiohttp
import pytest

from mycel.config import BackendConfig, LocationConfig, MeshConfig, NodeConfig
from mycel.governor import Limits
from mycel.identity import Identity, b64e
from mycel.node import Node
from mycel.relay import Relay, RelayLimits, auth_message
from mycel.rpc import KxKey, SealError, StreamOpener, StreamSealer, open_request, seal_request
from mycel.trust import mint_ticket

HW = {"accelerator": "cuda", "device_name": "RTX 4090", "accel_memory_gb": 24.0, "system_memory_gb": 32.0, "bandwidth_gbs": 1008.0}


def run(c):
    return asyncio.run(c)


def test_sealed_requests_and_streams_resist_tampering():
    kx, ident = KxKey.generate(), Identity.generate()
    env = ident.seal("infer", {"q": "secret prompt"}, to="bob")
    blob, rk = seal_request(kx.public_b64, "bob", "infer", env)
    assert b"secret" not in blob
    with pytest.raises(SealError):
        open_request(kx, "carol", blob)            # bound to the intended recipient
    with pytest.raises(SealError):
        open_request(KxKey.generate(), "bob", blob)
    method, got, rk2 = open_request(kx, "bob", blob)
    assert method == "infer" and got == env and rk2 == rk
    sealer, opener = StreamSealer(rk), StreamOpener(rk)
    a, b = sealer.seal({"delta": "one"}), sealer.seal({"delta": "two"})
    with pytest.raises(SealError):
        StreamOpener(rk).open(b)                    # reordered
    flipped = bytearray(a); flipped[3] ^= 1
    with pytest.raises(SealError):
        StreamOpener(rk).open(bytes(flipped))       # modified
    assert opener.open(a) == {"delta": "one"} and opener.open(b) == {"delta": "two"}


async def _ws_auth(url, ident, ticket, sign=True):
    s = aiohttp.ClientSession()
    ws = await s.ws_connect(url)
    ch = await ws.receive_json()
    sig = ident.private_key.sign(auth_message(ch["nonce"], ch["relay_id"] if sign else "other"))
    await ws.send_json({"t": "auth", "node": ident.node_id, "pubkey": ident.pubkey, "sig": b64e(sig), "ticket": ticket})
    return s, ws, await ws.receive_json()


def test_relay_admits_only_proven_identities_and_rate_limits():
    async def main():
        relay = Relay(limits=RelayLimits(frames_per_s=5, burst_frames=5))
        await relay.start()
        try:
            good = Identity.generate()
            s, ws, msg = await _ws_auth(relay.url, good, mint_ticket(good))
            assert msg["t"] == "welcome"
            other = Identity.generate()
            for ticket, sign in ((None, True), ({"mesh": "global", "pubkey": other.pubkey, "nonce": 1, "bits": 8}, True),
                                 (mint_ticket(other), False)):
                s2, _, bad = await _ws_auth(relay.url, other, ticket, sign)
                assert bad["t"] == "error"
                await s2.close()
            assert relay.stats["auth_failed"] == 3
            from mycel.relay import REQ, pack
            for _ in range(20):
                await ws.send_bytes(pack(REQ, "nobody", b"12345678", b"x"))
            await asyncio.sleep(0.3)
            assert relay.stats["rate_limited"] > 0      # frames beyond the bucket are dropped and nacked
            await s.close()
        finally:
            await relay.stop()
    run(main())


def _cfg(name, relay, **kw):
    return NodeConfig(name=name, data_dir=tempfile.mkdtemp(), control_port=0, probe_hardware=False, simulated_hardware=HW,
                      mesh=MeshConfig(listen=False, relays=[relay], gossip_interval_s=0.25, advert_ttl_s=5.0),
                      limits=Limits(max_share=1.0, per_peer_per_min=1000),
                      backend=BackendConfig(kind="mock", models=["m"], mock_tokens_per_s=200), **kw)


def test_private_by_default_and_opt_in_disclosure():
    async def main():
        relay = Relay(audit=True)
        await relay.start()
        a = Node(_cfg("a", relay.url, location=LocationConfig(lat=51.5, lon=-0.1)), Identity.generate())
        b = Node(_cfg("b", relay.url), Identity.generate())
        for n in (a, b):
            await n.join_global()
            await n.start()
        try:
            for _ in range(80):
                if b.membership.peers.get(a.id):
                    break
                await asyncio.sleep(0.1)
            body = b.membership.peers[a.id].body
            assert body["hw"] == {"tier": "T2", "simulated": True}           # no device name, memory or bandwidth
            assert body["location"] is None and "served" not in body and "addr" not in body   # no location, no IP
            assert body["relays"] and body["kx"]
            async with aiohttp.ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{a.control_port}/api/visibility", json={"device": True}) as r:
                    view = await r.json()
            assert view["advert"]["body"]["hw"]["device_name"] == "RTX 4090"
            for _ in range(80):
                if (b.membership.peers[a.id].body.get("hw") or {}).get("device_name"):
                    break
                await asyncio.sleep(0.1)
            assert b.membership.peers[a.id].body["hw"]["device_name"] == "RTX 4090"
            # Chat across the relay: E2E, so the relay never sees the prompt.
            out = [ev async for ev in b.route("m", [{"role": "user", "content": "the quiet harbor plan"}], 16, 0.0, prefer_local=False, exclude_self=True)]
            assert out[-1]["path"]["transport"] == "relay" and out[-1]["path"]["e2e"]
            assert not any(b"quiet harbor" in f for f in relay.audit_log)
        finally:
            for n in (a, b):
                await n.stop()
            await relay.stop()
    run(main())


def test_express_lane_results_cannot_be_spoofed():
    async def main():
        from mycel.swarm.client import SwarmClient

        c = SwarmClient(None)
        q = c.expect("t1", ["s0", "s1"])
        async for _ in c.rpc_result("intruder", {"tag": "t1", "idx": 1, "x": "", "shape": [1, 1]}):
            pass
        async for _ in c.rpc_result("s0", {"tag": "t1", "idx": 1, "error": "next stage down"}):   # predecessor may report
            pass
        async for _ in c.rpc_result("s1", {"tag": "t1", "idx": 1, "x": "", "shape": [1, 1]}):
            pass
        got = [q.get_nowait() for _ in range(q.qsize())]
        assert [("error" in g) for g in got] == [True, False]
    run(main())



def test_a_private_model_is_invisible_to_the_mesh():
    async def main():
        relay = Relay()
        await relay.start()
        a, b = Node(_cfg("a", relay.url), Identity.generate()), Node(_cfg("b", relay.url), Identity.generate())
        for n in (a, b):
            await n.join_global()
            await n.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{a.control_port}/api/limits", json={"paused": True}) as r:
                    assert r.status == 200
                async with s.get(f"http://127.0.0.1:{a.control_port}/api/status") as r:
                    st = await r.json()
            assert st["serving"] and not st["sharing"]          # still runs for its owner...
            for _ in range(80):
                body = (b.membership.peers.get(a.id) or type("x", (), {"body": {}})).body
                if body and body.get("role") == "client":
                    break
                await asyncio.sleep(0.1)
            assert body["models"] == [] and body["role"] == "client" and body["tps"] == 0.0   # ...but isn't even named
            own = [ev async for ev in a.route("m", [{"role": "user", "content": "hi"}], 8, 0.0)]
            assert own[-1]["mode"] == "local"                  # the owner's chats still use it
        finally:
            for n in (a, b):
                await n.stop()
            await relay.stop()
    run(main())

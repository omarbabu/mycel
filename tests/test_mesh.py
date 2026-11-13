"""End-to-end: real nodes on loopback ephemeral ports talking over HTTP."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import aiohttp

from mycel.config import BackendConfig, LocationConfig, MeshConfig, NodeConfig
from mycel.governor import Limits
from mycel.identity import Identity
from mycel.node import Node, RemoteError
from mycel.trust import issue_cert, membership_code

HW = {"accelerator": "cuda", "device_name": "test", "accel_memory_gb": 24.0, "tflops_fp16": 20.0, "system_memory_gb": 32.0}
FAKE_MCP = str(Path(__file__).parent / "fake_mcp_server.py")


def cfg(name, models=None, tps=200.0, share=1.0, location=None):
    return NodeConfig(
        name=name, data_dir=tempfile.mkdtemp(prefix="cm-test-"), control_port=0, probe_hardware=False,
        serve=bool(models), simulated_hardware=HW,
        mesh=MeshConfig(port=0, gossip_interval_s=0.2, advert_ttl_s=3.0),
        limits=Limits(max_share=share, max_concurrent=4, per_peer_per_min=1000),
        location=location or LocationConfig(),
        backend=BackendConfig(kind="mock", models=models or ["unused"], mock_tokens_per_s=tps),
    )


def admit(founder: Node, node: Node, can_invite=False):
    chain = issue_cert(founder.identity, founder.trust.chain, node.id, node.identity.pubkey, can_invite)
    node.join_private(membership_code(chain, [founder.peer_entry()]))


async def converge(nodes, timeout=10):
    for _ in range(int(timeout * 10)):
        if all(len(n.membership.alive()) == len(nodes) - 1 for n in nodes):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("mesh did not converge: " + str([len(n.membership.alive()) for n in nodes]))


async def start_mesh(*cfgs):
    """The default: everyone joins the founderless global mesh via a seed peer."""
    nodes = [Node(c, Identity.generate()) for c in cfgs]
    await nodes[0].join_global()
    await nodes[0].start()
    for n in nodes[1:]:
        n.cfg.mesh.global_bootstrap = [nodes[0].peer_entry()]
        await n.join_global()
        await n.start()
    await converge(nodes)
    return nodes


async def start_private_mesh(*cfgs):
    nodes = [Node(c, Identity.generate()) for c in cfgs]
    nodes[0].create_private_mesh()
    await nodes[0].start()
    for n in nodes[1:]:
        admit(nodes[0], n)
        await n.start()
    await converge(nodes)
    return nodes


async def stop_all(nodes):
    for n in nodes:
        await n.stop()


async def api(node, method, path, body=None):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"http://127.0.0.1:{node.control_port}{path}", json=body) as r:
            return r.status, await r.json()


async def chat(node, model, **extra):
    return await api(node, "POST", "/v1/chat/completions",
                     {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16, **extra})


def run(coro):
    return asyncio.run(coro)


def test_routing_limits_and_receipts():
    async def main():
        client, small, big, stingy = await start_mesh(
            cfg("client"), cfg("small", ["m-small"]), cfg("big", ["m-big"]), cfg("stingy", ["m-small"], share=0.0))
        try:
            status, data = await chat(client, "m-big")
            assert status == 200, data
            assert data["mesh"]["served_by"] == big.id and data["mesh"]["receipt_verified"]
            assert data["choices"][0]["message"]["content"]
            for _ in range(5):  # stingy donates 0% and must never be picked
                status, data = await chat(client, "m-small")
                assert status == 200 and data["mesh"]["served_by"] == small.id
            assert stingy.counters["served_mesh"] == 0
            assert (await chat(client, "m-nonexistent"))[0] == 503
            status, data = await chat(client, "auto")  # auto picks some available model
            assert status == 200 and data["model"] in ("m-big", "m-small")
            assert (await api(small, "POST", "/api/limits", {"paused": True}))[0] == 200
            await asyncio.sleep(0.8)
            status, data = await chat(client, "m-small")
            assert status == 503, data
        finally:
            await stop_all([client, small, big, stingy])
    run(main())


def test_failover_to_next_node():
    async def main():
        client, fast, slow = await start_mesh(cfg("client"), cfg("fast", ["m"], tps=900), cfg("slow", ["m"], tps=100))
        try:
            await fast.stop()  # dies abruptly; client still believes it is alive
            status, data = await chat(client, "m")
            assert status == 200, data
            assert data["mesh"]["served_by"] == slow.id
        finally:
            await stop_all([client, slow])
    run(main())


def test_streaming_and_local_preference():
    async def main():
        a, b = await start_mesh(cfg("a", ["m"]), cfg("b", ["m"]))
        try:
            status, data = await chat(a, "m")
            assert data["mesh"]["local"] is True  # prompt stays on this machine by default
            status, data = await chat(a, "m", mesh={"exclude_self": True})
            assert data["mesh"]["served_by"] == b.id
            async with aiohttp.ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{a.control_port}/v1/chat/completions",
                                  json={"model": "m", "stream": True, "messages": [{"role": "user", "content": "x"}],
                                        "mesh": {"exclude_self": True}}) as r:
                    body = (await r.read()).decode()
            chunks = [json.loads(l[6:]) for l in body.split("\n") if l.startswith("data: {")]
            assert body.rstrip().endswith("[DONE]")
            assert chunks[-1]["mesh"]["served_by"] == b.id and chunks[-1]["mesh"]["receipt_verified"]
        finally:
            await stop_all([a, b])
    run(main())


def test_tool_calls_cross_the_mesh_and_run_locally():
    async def main():
        client, server = await start_mesh(cfg("client"), cfg("server", ["m"]))
        try:
            status, tools = await api(client, "GET", "/api/plugins/tools")
            specs = [t["spec"] for t in tools["tools"] if "spec" in t]
            msgs = [{"role": "user", "content": "what is 1499 * 12 / 7"}]
            status, data = await api(client, "POST", "/v1/chat/completions", {"model": "m", "messages": msgs, "tools": specs})
            assert status == 200, data
            choice = data["choices"][0]
            assert choice["finish_reason"] == "tool_calls" and data["mesh"]["receipt_verified"]
            call = choice["message"]["tool_calls"][0]
            assert call["function"]["name"] == "calculator__evaluate"
            # The tool runs on the requester's own node, not on the server that asked for it.
            status, out = await api(client, "POST", "/api/plugins/call",
                                    {"name": call["function"]["name"], "arguments": call["function"]["arguments"]})
            assert status == 200 and "2,569.714" in out["result"]
            msgs += [choice["message"], {"role": "tool", "tool_call_id": call["id"], "content": out["result"]}]
            status, data = await api(client, "POST", "/v1/chat/completions", {"model": "m", "messages": msgs, "tools": specs})
            assert "2,569.714" in data["choices"][0]["message"]["content"]
        finally:
            await stop_all([client, server])
    run(main())


def test_outsiders_without_valid_tickets_are_refused():
    async def main():
        a, b = await start_mesh(cfg("a"), cfg("b", ["m"]))
        # 1) A node that skipped the proof of work (forged ticket).
        cheat = Node(cfg("cheat", ["m"]), Identity.generate())
        cheat.trust.mesh_id, cheat.trust.ticket = "global", {"mesh": "global", "pubkey": cheat.identity.pubkey, "nonce": 0, "bits": 8}
        from mycel.trust import _work, GLOBAL_BITS
        while _work(cheat.identity.pubkey_raw, cheat.trust.ticket["nonce"]) >= GLOBAL_BITS:
            cheat.trust.ticket["nonce"] += 1  # make sure the nonce really is invalid
        cheat.bootstrap = [a.peer_entry()]
        # 2) A member of some private mesh trying to wander into the global one.
        private = Node(cfg("private", ["m"]), Identity.generate())
        private.create_private_mesh()
        private.bootstrap = [a.peer_entry()]
        await cheat.start(); await private.start()
        try:
            await asyncio.sleep(1.0)
            for intruder in (cheat, private):
                assert intruder.id not in a.membership.peers and intruder.id not in b.membership.peers
                assert not intruder.membership.peers  # learned nothing about the global mesh
            assert a.membership.rejected > 0
            # Even with b's address and encryption key, a non-member's sealed request is refused.
            try:
                await cheat.rpc_one(b.id, {"addr": b.advertise_addr}, "infer",
                                    {"request_id": "x", "model": "m", "messages": [{"role": "user", "content": "x"}]})
                raise AssertionError("non-member was served")
            except RemoteError as exc:
                assert exc.status == 403
        finally:
            await stop_all([a, b, cheat, private])
    run(main())


def test_global_mesh_has_no_founder_and_blocking_is_local():
    async def main():
        a, b, c = await start_mesh(cfg("a"), cfg("b", ["m"]), cfg("c", ["m"]))
        try:
            _, ms = await api(a, "GET", "/api/membership")
            assert ms["kind"] == "global" and not ms["founder"] and not ms["can_invite"]
            assert (await api(a, "POST", "/api/membership/revoke", {"node_id": b.id}))[0] == 403  # nobody can evict
            assert (await api(a, "POST", "/api/membership/invite", {"join_code": "x"}))[0] == 400
            assert (await api(a, "POST", "/api/membership/block", {"node_id": b.id}))[0] == 200
            assert b.id not in a.membership.peers
            for _ in range(5):
                status, data = await chat(a, "m")
                assert status == 200 and data["mesh"]["served_by"] == c.id  # a no longer uses b
            assert b.id in c.membership.peers  # ...but nobody else is affected
        finally:
            await stop_all([a, b, c])
    run(main())


def test_newcomer_joins_global_mesh_from_the_app():
    async def main():
        (seed,) = await start_mesh(cfg("seed", ["m"]))
        newbie = Node(cfg("newbie"), Identity.generate())
        newbie.cfg.mesh.global_bootstrap = [seed.peer_entry()]
        await newbie.start()
        try:
            _, ms = await api(newbie, "GET", "/api/membership")
            assert ms["member"] is False
            status, ms = await api(newbie, "POST", "/api/membership/join-global", {})
            assert status == 200 and ms["member"] and ms["kind"] == "global"
            await converge([seed, newbie])
            status, data = await chat(newbie, "m")
            assert status == 200 and data["mesh"]["served_by"] == seed.id
        finally:
            await stop_all([seed, newbie])
    run(main())


def test_private_mesh_invite_and_revocation():
    async def main():
        founder, member = await start_private_mesh(cfg("founder"), cfg("member", ["m"]))
        newbie = Node(cfg("newbie"), Identity.generate())
        await newbie.start()
        try:
            _, ms = await api(newbie, "GET", "/api/membership")
            assert ms["member"] is False
            status, inv = await api(founder, "POST", "/api/membership/invite", {"join_code": ms["join_code"]})
            assert status == 200, inv
            status, joined = await api(newbie, "POST", "/api/membership/join", {"code": inv["membership_code"]})
            assert status == 200 and joined["member"], joined
            await converge([founder, member, newbie])
            status, data = await chat(newbie, "m")
            assert status == 200 and data["mesh"]["served_by"] == member.id
            # A plain member can't invite or remove anyone.
            assert (await api(member, "POST", "/api/membership/invite", {"join_code": ms["join_code"]}))[0] == 400
            assert (await api(member, "POST", "/api/membership/revoke", {"node_id": newbie.id}))[0] == 403
            # The founder removes the newcomer; the revocation spreads by gossip.
            assert (await api(founder, "POST", "/api/membership/revoke", {"node_id": newbie.id}))[0] == 200
            for _ in range(50):
                if newbie.id not in member.membership.peers:
                    break
                await asyncio.sleep(0.1)
            assert newbie.id not in member.membership.peers
            await asyncio.sleep(0.5)
            assert (await chat(newbie, "m"))[0] == 503
        finally:
            await stop_all([founder, member, newbie])
    run(main())


def test_location_opt_in_and_out():
    async def main():
        a, b, c = await start_mesh(
            cfg("a"),
            cfg("b", ["m"], location=LocationConfig(share=True, lat=6.5, lon=3.4, precision="region")),
            cfg("c", ["m"], location=LocationConfig(share=False, lat=52.5, lon=13.4)))
        try:
            _, data = await api(a, "GET", "/api/nodes")
            nodes = {n["node_id"]: n for n in data["nodes"]}
            assert len(nodes[b.id]["location"]) == 3 and nodes[b.id]["lat"] is not None
            assert nodes[c.id]["location"] is None and nodes[c.id]["lat"] is None
            raw = json.dumps([p.advert for p in a.membership.peers.values()])
            assert "52.5" not in raw and "6.5," not in raw  # coordinates never on the wire
        finally:
            await stop_all([a, b, c])
    run(main())


def test_plugins_permissions_and_mcp_isolation():
    async def main():
        (a,) = [Node(cfg("a", ["m"]), Identity.generate())]
        await a.start()
        os.environ["CM_TEST_SECRET"] = "hunter2"
        try:
            status, data = await api(a, "POST", "/api/plugins/call", {"name": "notes__add", "arguments": {"text": "x"}})
            assert status == 403  # notes asks before each use
            status, data = await api(a, "POST", "/api/plugins/call",
                                     {"name": "notes__add", "arguments": {"text": "buy milk"}, "approved": True})
            assert status == 200 and data["ok"]
            cmd = [sys.executable, FAKE_MCP]
            assert (await api(a, "POST", "/api/plugins/custom", {"name": "Fake", "command": cmd}))[0] == 400  # no confirm
            status, out = await api(a, "POST", "/api/plugins/custom", {"name": "Fake", "command": cmd, "confirm": True})
            assert status == 200
            pid = out["id"]
            _, tools = await api(a, "GET", "/api/plugins/tools")
            names = {t["spec"]["function"]["name"] for t in tools["tools"] if "spec" in t}
            assert f"{pid}__echo" in names
            status, data = await api(a, "POST", "/api/plugins/call",
                                     {"name": f"{pid}__echo", "arguments": {"text": "hello"}, "approved": True})
            assert data["result"] == "hello"
            _, data = await api(a, "POST", "/api/plugins/call", {"name": f"{pid}__env", "arguments": {}, "approved": True})
            assert data["result"] == "clean"  # secrets in our environment are not passed to plugins
            # Catalog MCP plugins need the exact command echoed back as consent.
            assert (await api(a, "POST", "/api/plugins/install", {"id": "memory"}))[0] == 400
            assert (await api(a, "POST", "/api/plugins/uninstall", {"id": pid}))[0] == 200
        finally:
            os.environ.pop("CM_TEST_SECRET", None)
            await a.stop()
    run(main())


def test_conversations_and_guard():
    async def main():
        (a,) = [Node(cfg("a", ["m"]), Identity.generate())]
        await a.join_global()
        await a.start()
        base = f"http://127.0.0.1:{a.control_port}"
        try:
            status, c = await api(a, "POST", "/api/conversations", {"title": "Trip"})
            assert status == 200
            msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
            assert (await api(a, "PUT", f"/api/conversations/{c['id']}", {"messages": msgs}))[0] == 200
            _, lst = await api(a, "GET", "/api/conversations")
            assert lst["conversations"][0]["preview"] == "hello"
            assert (await api(a, "GET", "/api/conversations/..%2Fmembership"))[0] == 404
            assert (await api(a, "DELETE", f"/api/conversations/{c['id']}"))[0] == 200
            _, models = await api(a, "GET", "/v1/models")
            assert models["data"][0]["id"] == "auto"
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/status", headers={"Host": "evil.example"}) as r:
                    assert r.status == 403  # DNS rebinding
                async with s.post(base + "/api/limits", json={"paused": True}, headers={"Origin": "http://evil.example"}) as r:
                    assert r.status == 403  # cross-site
                async with s.post(base + "/api/limits", data="paused=true",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
                    assert r.status == 415  # form CSRF
                async with s.get(base + "/") as r:
                    assert r.status == 200 and "default-src 'self'" in r.headers["Content-Security-Policy"]
            assert a.governor.limits.paused is False
        finally:
            await a.stop()
    run(main())


def test_prefers_nodes_whose_budget_covers_the_job():
    async def main():
        client, tight, roomy = await start_mesh(
            cfg("client"), cfg("tight", ["m"], tps=2000, share=0.0167),  # ~1.0 s bucket < 1 s overhead + tokens
            cfg("roomy", ["m"], tps=100, share=1.0))
        try:
            for _ in range(4):
                status, data = await chat(client, "m")
                assert status == 200 and data["mesh"]["served_by"] == roomy.id, data
                assert data["mesh"]["clamped_to"] is None
        finally:
            await stop_all([client, tight, roomy])
    run(main())


FAKE_LLAMA = str(Path(__file__).parent / "fake_llama_server.py")


def test_model_library_download_serve_chat_and_resume():
    async def main():
        c = cfg("real")
        c.backend = BackendConfig(kind="managed", server_cmd=[sys.executable, FAKE_LLAMA])
        c.serve = True
        node = Node(c, Identity.generate())
        await node.join_global()
        await node.start()
        try:
            _, lib = await api(node, "GET", "/api/library")
            assert lib["serving"] is False and not lib["models"][0]["downloaded"]
            small = next(m for m in lib["models"] if m["id"] == "qwen3-1.7b")
            assert small["fits_device"] and small["est_tokens_per_s"] is None  # test hardware has no measured bandwidth
            assert (await api(node, "POST", "/api/library/serve", {"id": "qwen3-1.7b"}))[0] == 200
            seen = set()
            for _ in range(100):
                _, lib = await api(node, "GET", "/api/library")
                seen.add(lib["server"]["status"])
                if lib["serving"]:
                    break
                await asyncio.sleep(0.1)
            assert lib["serving"] and "downloading" in seen, (seen, lib["server"])
            assert next(m for m in lib["models"] if m["id"] == "qwen3-1.7b")["serving_here"]
            status, data = await chat(node, "auto")  # auto -> the model this device serves
            assert status == 200 and data["model"] == "qwen3-1.7b" and data["mesh"]["local"]
            assert "running on this device" in data["choices"][0]["message"]["content"]
            assert "greeting" in data["choices"][0]["message"]["reasoning_content"]  # thinking is passed through
            assert (await api(node, "POST", "/api/limits", {"max_share": 0.5}))[0] == 200
        finally:
            await node.stop()
        # Restart: settings and the served model come back on their own.
        node2 = Node(c, node.identity)
        await node2.start()
        try:
            assert node2.governor.limits.max_share == 0.5
            for _ in range(100):
                if node2.serving:
                    break
                await asyncio.sleep(0.1)
            assert node2.serving and node2.backend.models == ["qwen3-1.7b"]
            assert (await api(node2, "POST", "/api/library/stop", {}))[0] == 200
            assert not node2.serving and node2.serve_model_id is None
        finally:
            await node2.stop()
    run(main())


def test_reasoning_crosses_the_mesh_with_a_valid_receipt():
    async def main():
        client, server = await start_mesh(cfg("client"), cfg("server", ["m"]))
        try:
            status, data = await api(client, "POST", "/v1/chat/completions",
                                     {"model": "m", "messages": [{"role": "user", "content": "think about mesh networks"}]})
            assert status == 200 and data["mesh"]["receipt_verified"] and data["mesh"]["served_by"] == server.id
            assert data["choices"][0]["message"]["reasoning_content"] == "Let me think this through."
        finally:
            await stop_all([client, server])
    run(main())

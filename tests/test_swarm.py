"""Split (swarm) inference: exactness, placement, routing, failover, speculation, cross-checks."""
import asyncio
import tempfile
import time
from pathlib import Path

import numpy as np
from aiohttp import web

from mycel import models as catalog
from mycel.config import BackendConfig, MeshConfig, NodeConfig, SwarmConfig
from mycel.governor import Limits
from mycel.identity import Identity
from mycel.node import Node, RouteError
from mycel.relay import Relay
from mycel.swarm import planner
from mycel.swarm.engine import GGUFStage, LocalModel
from mycel.swarm.gguf import GGUF, fetch_slice
from mycel.swarm.tiny import make_tiny_gguf
from mycel.swarm.tokenizer import OutputParser, Tokenizer, render_chat

TMP = Path(tempfile.mkdtemp(prefix="mycel-swarm-"))
TARGET = make_tiny_gguf(TMP / "target.gguf", arch="llama", layers=8, seed=0)
OTHER_DRAFT = make_tiny_gguf(TMP / "draft.gguf", arch="llama", layers=2, seed=3)
QWEN = make_tiny_gguf(TMP / "qwen.gguf", arch="qwen3", layers=6, seed=1)
for mid, arch, layers, draft in (("tiny-llama", "llama", 8, "tiny-draft"), ("tiny-draft", "llama", 2, None),
                                  ("tiny-same", "llama", 8, "tiny-llama"), ("tiny-mock", "llama", 80, None)):
    catalog.register(catalog.CatalogModel(mid, mid, "test", 8.0, 8.0, f"{mid}.gguf", f"test/{mid}", 8.0, 1, "MIT", "",
                                          arch=arch, layers=layers, hidden=64, vocab=300, draft=draft))
FILES = {"tiny-llama": str(TARGET), "tiny-draft": str(OTHER_DRAFT), "tiny-same": str(TARGET)}
HW = {"accelerator": "cuda", "device_name": "test", "accel_memory_gb": 24.0, "system_memory_gb": 32.0, "bandwidth_gbs": 400.0}
MSGS = [{"role": "user", "content": "the swarm is in tune"}]


def run(coro):
    return asyncio.run(coro)


def reference(path, msgs, n, arch="llama"):
    g = GGUF(path)
    tok, m = Tokenizer.from_gguf(g), LocalModel(g)
    ids = tok.encode(render_chat(arch, msgs))
    lg, pos, out = m.step(ids, 0), len(ids), []
    for _ in range(n):
        t = int(np.argmax(lg[-1]))
        if t in tok.stop_ids():
            break
        out.append(t)
        lg, pos = m.step([t], pos), pos + 1
    return tok.decode(out)


# Real-weight engines size slices by their true (fp32) footprint, so tiny test models pick an explicit
# layer count; the old budgets map to: 3.0 GB -> 2 layers, 3.4 -> 3, 4.5 -> 4, 100 -> all 8.
LAYERS_FOR_BUDGET = {3.0: 2, 3.4: 3, 4.5: 4, 100: 8}


def cfg(name, relay_url, model=None, budget=3.0, engine="gguf", draft=False, crosscheck=True):
    layers = LAYERS_FOR_BUDGET.get(budget, 0) if engine == "gguf" else 0
    if layers:
        budget = 1000.0
    return NodeConfig(
        name=name, data_dir=tempfile.mkdtemp(prefix="node-", dir=TMP), control_port=0, probe_hardware=False, serve=False, simulated_hardware=HW,
        mesh=MeshConfig(listen=False, relays=[relay_url], gossip_interval_s=0.25, advert_ttl_s=5.0),
        limits=Limits(max_share=1.0, per_peer_per_min=1000),
        swarm=SwarmConfig(model=model, budget_gb=budget, layers=layers, engine=engine, files=FILES, rebalance_s=600, draft=draft,
                          crosscheck=crosscheck),
        backend=BackendConfig(kind="mock" if engine == "mock" else "none", models=["none"], server_cmd=["true"]))


async def swarm(n_hosts, model="tiny-llama", budget=3.0, engine="gguf", draft=False, byzantine=(), crosscheck=True,
                min_replicas=1):
    relay = Relay(audit=True)
    await relay.start()
    client = Node(cfg("client", relay.url, draft=draft, engine=engine, crosscheck=crosscheck), Identity.generate())
    hosts = [Node(cfg(f"h{i}", relay.url, model, budget, engine), Identity.generate()) for i in range(n_hosts)]
    for i in byzantine:
        hosts[i].swarm_host.byzantine = True
    for n in [client, *hosts]:
        await n.join_global()
        await n.start()
    L = catalog.BY_ID[model].layers
    stable, last = 0, None
    for _ in range(400):
        ov = client.swarm_overview().get(model)
        placement = tuple(n.swarm_host.span for n in hosts)
        stable = stable + 1 if placement == last else 0
        last = placement
        if ov and ov["complete"] and ov["hosts"] == n_hosts and ov["min_replicas"] >= min_replicas \
                and all(len(n.membership.alive()) == n_hosts for n in hosts) and stable >= 10:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"swarm never assembled: {client.swarm_overview()}")
    assert sum(1 for c in client.swarm_overview()[model]["coverage"] if c) == L
    return relay, client, hosts


async def teardown(relay, *nodes):
    for n in nodes:
        await n.stop()
    await relay.stop()


async def collect(node, model, n=20, temperature=0.0):
    text, done, progress = [], None, []
    async for ev in node.route(model, MSGS, n, temperature):
        if "delta" in ev:
            text.append(ev["delta"])
        elif "progress" in ev:
            progress.append(ev)
        elif ev.get("done"):
            done = ev
    return "".join(text), done, progress


# ---------------------------------------------------------------------------- the math
def test_splitting_layers_across_stages_is_bit_exact():
    for path in (TARGET, QWEN):
        g = GGUF(path)
        L = g.layer_count()
        whole = GGUFStage(g, 0, L)
        parts = [GGUFStage(g, 0, 3), GGUFStage(g, 3, 5), GGUFStage(g, 5, L)]
        cw, cps = whole.new_cache(), [p.new_cache() for p in parts]
        x = np.random.default_rng(0).standard_normal((7, 64)).astype(np.float32)
        pos = 0
        for step in range(5):
            a = whole.forward(x, pos, cw)
            b = x
            for p, c in zip(parts, cps):
                b = p.forward(b, pos, c)
            assert np.array_equal(a, b), f"{path.name} diverged at step {step}"
            pos += x.shape[0]
            x = a[-1:]
        # Truncating the cache (speculative rollback) then recomputing gives the same answer.
        again = whole.forward(x, pos, cw)
        whole.forward(np.ones((3, 64), np.float32), pos, cw)
        assert np.array_equal(whole.forward(x, pos, cw), again)


def test_output_parser_splits_thinking_tool_calls_and_text():
    p = OutputParser()
    evs = []
    for piece in ["<thi", "nk>hm</think>\n\nHi ", "<tool_", 'call>\n{"name": "calc", "arguments": {"x": 1}}\n</tool_call>']:
        evs += p.feed(piece)
    evs += p.flush()
    assert {"reasoning": "hm"} in evs and {"delta": "Hi "} in evs
    assert p.calls[0]["function"]["name"] == "calc" and '"x": 1' in p.calls[0]["function"]["arguments"]


# ---------------------------------------------------------------------------- placement & routing
def S(node, a, b, ms=1.0, rtt=10.0):
    return planner.Span(node, "m", a, b, ms, rtt)


def test_placement_fills_holes_then_adds_replicas_and_moves_only_with_hysteresis():
    others = [S("a", 0, 4), S("b", 4, 8)]
    assert planner.choose_span(others, 12, 4, "me") == (8, 12)            # the hole first
    full = others + [S("c", 8, 12)]
    st = planner.choose_span(full, 12, 4, "me")
    assert st in [(0, 4), (4, 8), (8, 12)]                                  # then a replica, aligned
    # A node whose layers are also held by someone else moves into a hole; a sole host never does.
    assert planner.better_span([S("x", 0, 4), S("y", 4, 8)], 12, 4, "a", (0, 4)) == (8, 12)
    assert planner.better_span([S("y", 4, 8)], 12, 4, "a", (0, 4)) is None
    # Of two hosts of the same layers, only the lower id may move — they can't both leave at once...
    assert planner.better_span([S("x", 0, 4), S("y", 4, 8)], 12, 4, "z", (0, 4)) is None
    # ...but a lower-id neighbour that can't move itself doesn't block anyone (no deadlock).
    stuck = [S("a", 5, 8), S("m", 3, 6), S("n", 3, 6), S("p", 0, 3), S("q", 0, 3)]
    assert planner.better_span(stuck, 8, 3, "o", (3, 6)) is None          # "m" (lower id, movable) goes first
    assert planner.better_span(stuck[:1] + stuck[2:] + [S("o", 3, 6)], 8, 3, "m", (3, 6)) == (5, 8)


def test_planner_prefers_few_fast_nearby_hops_and_reports_gaps():
    spans = [S("near", 0, 6, rtt=5), S("far", 0, 6, rtt=200), S("n2", 6, 12, rtt=5),
             S("tiny1", 0, 3, rtt=5), S("tiny2", 3, 6, rtt=5)]
    chain = planner.plan(spans, 12)
    assert [s.node for s, _, _ in chain] == ["near", "n2"]
    assert planner.plan(spans, 12, exclude={"near"})[0][0].node == "tiny1"   # routes around a failure
    assert planner.plan([S("a", 0, 5)], 12) is None
    assert planner.missing_ranges(planner.coverage([S("a", 0, 5), S("b", 8, 12)], 12)) == [(5, 8)]


# ---------------------------------------------------------------------------- end to end over relays
def test_swarm_over_relays_is_exact_private_and_traced():
    async def main():
        relay, client, hosts = await swarm(4)
        try:
            text, done, progress = await collect(client, "tiny-llama@swarm")
            assert text == reference(TARGET, MSGS, 20) and text
            path = done["path"]
            assert done["mode"] == "swarm" and path["mode"] == "swarm" and path["text_stays_local"]
            spans = [h["layers"] for h in path["hops"]]
            assert spans[0][0] == 0 and spans[-1][1] == 8 and all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
            assert all(h["transport"] == "relay" and h["device"] is None for h in path["hops"])  # hardware private by default
            assert progress[0]["progress"] == "route" and any(p["progress"] == "prefill" for p in progress)
            # Every byte through the relay was ciphertext: neither the prompt nor the reply appears.
            assert relay.audit_log and not any(b"swarm is in tune" in f or text[:6].encode() in f for f in relay.audit_log)
            # No node has a listening port; all of them are reachable anyway.
            assert all(n.http is None for n in [client, *hosts])
        finally:
            await teardown(relay, client, *hosts)
    run(main())


def test_swarm_survives_a_stage_dying_mid_reply():
    async def main():
        relay, client, hosts = await swarm(6, budget=3.4, min_replicas=2)   # every layer has a replica
        try:
            expect = reference(TARGET, MSGS, 30)
            text, killed, done, route = [], None, None, None
            async for ev in client.route("tiny-llama@swarm", MSGS, 30, 0.0):
                if ev.get("progress") == "route":
                    route = ev["hops"]
                if "delta" in ev:
                    text.append(ev["delta"])
                    if len(text) == 5 and killed is None:   # pull the plug on the second stage
                        killed = next(n for n in hosts if n.id == route[1]["node"])
                        await killed.stop()
                if ev.get("done"):
                    done = ev
            assert "".join(text) == expect
            fo = [e for e in done["path"]["events"] if e["kind"] == "failover"]
            assert fo and fo[0]["node"] == killed.id and fo[0]["replayed_tokens"] > 0
        finally:
            await teardown(relay, client, *[h for h in hosts if h is not killed])
    run(main())


def test_speculative_decoding_is_exact():
    async def main():
        relay, client, hosts = await swarm(3, model="tiny-same", budget=3.4, draft=True)   # draft == target
        try:
            text, done, _ = await collect(client, "tiny-same@swarm", 24)
            assert text == reference(TARGET, MSGS, 24)
            spec = done["path"]["speculative"]
            assert spec["accepted"] == spec["proposed"] > 0 and spec["rounds"] < 24   # several tokens per round trip
        finally:
            await teardown(relay, client, *hosts)
        relay, client, hosts = await swarm(3, budget=3.4, draft=True)   # an unrelated draft: rejections, same output
        try:
            text, done, _ = await collect(client, "tiny-llama@swarm", 24)
            assert text == reference(TARGET, MSGS, 24)
            assert done["path"]["speculative"]["accepted"] < done["path"]["speculative"]["proposed"]
        finally:
            await teardown(relay, client, *hosts)
    run(main())


def test_cross_check_catches_a_lying_stage():
    async def main():
        # Three nodes each hold the whole (simulated) 80-layer model; one returns wrong math.
        relay, client, hosts = await swarm(3, model="tiny-mock", budget=1000, engine="mock", byzantine=(0,))
        try:
            liars = set()
            for _ in range(12):
                _, done, _ = await collect(client, "tiny-mock@swarm", 6)
                ev = done["path"].get("crosscheck")
                if ev and not ev["ok"]:
                    liars.add(ev["liar"])
            assert liars == {hosts[0].id}
            assert client.membership.peers[hosts[0].id].failed >= 10   # down-ranked locally
        finally:
            await teardown(relay, client, *hosts)
    run(main())


def test_incomplete_swarm_explains_what_is_missing():
    async def main():
        relay, client, hosts = await swarm(1, model="tiny-llama", budget=100)   # one node holds all 8 layers
        try:
            await hosts[0].swarm_host.stop()
            hosts[0].swarm_host.engine, hosts[0].swarm_host.span = None, None
            hosts[0].swarm_host.status = "idle"
            await asyncio.sleep(1.0)
            try:
                await collect(client, "tiny-llama@swarm")
                raise AssertionError("expected a RouteError")
            except RouteError as exc:
                assert "no free host" in str(exc) or "no node" in str(exc) or "can't run" in str(exc)
        finally:
            await teardown(relay, client, *hosts)
    run(main())


def test_fetch_slice_downloads_only_that_slice():
    async def main():
        app = web.Application()
        app.router.add_get("/m.gguf", lambda r: web.FileResponse(TARGET))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[-1][1]
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                dest = await fetch_slice(s, f"http://127.0.0.1:{port}/m.gguf", TMP / "slice.gguf", 2, 4)
        finally:
            await runner.cleanup()
        g, full = GGUF(dest), GGUF(TARGET)
        assert {t.layer for t in g.tensors.values()} == {2, 3}
        assert dest.stat().st_size < TARGET.stat().st_size / 3
        x = np.random.default_rng(1).standard_normal((3, 64)).astype(np.float32)
        a, b = GGUFStage(g, 2, 4), GGUFStage(full, 2, 4)
        assert np.array_equal(a.forward(x, 0, a.new_cache()), b.forward(x, 0, b.new_cache()))
    run(main())


PLAIN_TPS_BAR = 3.0


def test_simulated_70b_swarm_meets_the_speed_bar_over_wan():
    """Plan acceptance: a 70B-class model over >= 6 nodes with 8 GB GPUs at ~30 ms RTT sustains
    >= 3 tok/s (>= 5 with speculative decoding), simulated compute, real protocol and relay."""
    async def main():
        from mycel.transport import Netem
        relay = Relay()
        await relay.start()
        hw8 = {**HW, "accel_memory_gb": 8.0, "bandwidth_gbs": 448.0, "device_name": "RTX 3070"}
        nodes = []
        for i in range(9):
            c = cfg(f"w{i}", relay.url, "llama-3.3-70b" if i else None, budget=6.5, engine="mock", draft=i == 0)
            c.simulated_hardware = hw8
            n = Node(c, Identity.generate())
            n.netem = Netem(lambda a, b: 0.015, jitter=0.1)   # 15 ms each way -> ~30 ms RTT via the relay
            nodes.append(n)
        for n in nodes:
            await n.join_global()
            await n.start()
        try:
            for _ in range(200):
                ov = nodes[0].swarm_overview().get("llama-3.3-70b")
                if ov and ov["complete"]:
                    break
                await asyncio.sleep(0.1)
            assert ov and ov["complete"] and ov["hosts"] == 8, ov   # 11 of 80 layers fit in 6.5 GB
            await asyncio.sleep(1.5)  # a few gossip rounds measure RTTs
            for spec in (False, True):
                nodes[0].cfg.swarm.draft = spec
                _, done, _ = await collect(nodes[0], "llama-3.3-70b@swarm", 60)
                assert len(done["path"]["hops"]) >= 6
                assert done["path"]["express"]["steps"] > 0 and done["path"]["express"]["fallbacks"] == 0
                print(f"\n70B sim: speculative={spec} {done['tokens_per_s']} tok/s, {len(done['path']['hops'])} hops")
                assert done["tokens_per_s"] >= (5.0 if spec else PLAIN_TPS_BAR), (spec, done["tokens_per_s"])
        finally:
            await teardown(relay, *nodes)
    run(main())


def test_gpu_engine_splits_exactly_and_matches_the_cpu_engine():
    import pytest
    from mycel.swarm.engine import TorchStage, torch_device
    if not torch_device():
        pytest.skip("no Metal/CUDA device")
    g = GGUF(QWEN)
    whole, parts = TorchStage(g, 0, 6), [TorchStage(g, 0, 2), TorchStage(g, 2, 6)]
    cpu = GGUFStage(g, 0, 6)
    cw, cps, cc = whole.new_cache(), [p.new_cache() for p in parts], cpu.new_cache()
    x = np.random.default_rng(0).standard_normal((5, 64)).astype(np.float32)
    pos = 0
    for _ in range(4):
        a, ref = whole.forward(x, pos, cw), cpu.forward(x, pos, cc)
        b = x
        for p, c in zip(parts, cps):
            b = p.forward(b, pos, c)
        assert np.array_equal(a, b)                                   # splitting is exact on the GPU too
        assert np.max(np.abs(a - ref)) < 2e-2 * (np.max(np.abs(ref)) + 1)   # and agrees with fp32 to fp16 precision
        pos += x.shape[0]
        x = a[-1:]


def test_hosts_and_requesters_fetch_only_their_slices_over_http():
    """No local weights anywhere: each host range-downloads its own layers, the requester just the
    embedding/head — from a loopback stand-in for Hugging Face."""
    async def main():
        app = web.Application()
        hits = []

        async def serve(r):
            hits.append(r.headers.get("Range"))
            return web.FileResponse(TARGET)
        app.router.add_get("/test/tiny-llama/resolve/main/tiny-llama.gguf", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        base = f"http://127.0.0.1:{runner.addresses[-1][1]}"
        relay = Relay()
        await relay.start()
        nodes = []
        try:
            for i in range(3):
                c = cfg(f"d{i}", relay.url, "tiny-llama" if i else None, budget=4.5)
                c.swarm.files, c.swarm.weights_url = {}, base
                nodes.append(Node(c, Identity.generate()))
            for n in nodes:
                await n.join_global()
                await n.start()
            for _ in range(200):
                ov = nodes[0].swarm_overview().get("tiny-llama")
                if ov and ov["complete"] and ov["hosts"] == 2:
                    break
                await asyncio.sleep(0.1)
            assert ov and ov["complete"], ov
            text, done, progress = await collect(nodes[0], "tiny-llama@swarm", 16)
            assert text == reference(TARGET, MSGS, 16)
            assert any(p["progress"] == "bookends" for p in progress)
            slices = [p for n in nodes for p in (n.data_dir / "slices").glob("*.gguf")]
            assert len(slices) >= 3 and all(p.stat().st_size < TARGET.stat().st_size for p in slices)
            assert all(h and h.startswith("bytes=") for h in hits)
        finally:
            await teardown(relay, *nodes)
            await runner.cleanup()
    run(main())


def test_a_saved_slice_that_no_longer_fits_never_stops_the_node():
    async def main():
        relay = Relay()
        await relay.start()
        c = cfg("s", relay.url, "llama-3.3-70b", budget=0, engine="mock")
        c.simulated_hardware = {**HW, "accel_memory_gb": 2.0}   # far too small for even one layer
        n = Node(c, Identity.generate())
        await n.join_global()
        await n.start()                                         # must not raise
        try:
            snap = n.swarm_host.snapshot()
            assert snap["status"] == "error" and snap["error"]
        finally:
            await n.stop()
            await relay.stop()
    run(main())

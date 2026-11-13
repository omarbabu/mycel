"""Multi-process internet-mesh harness, entirely on loopback.

R relay processes + W worker processes hosting N nodes in total. Every node is behind
simulated NAT (no listening port), reaches others only via relays, and sees injected WAN
latency (20–120 ms one-way) plus occasional retransmit stalls. During the run the harness
churns nodes and kills one relay outright, and reports:

* convergence (how much of the mesh each node sees) over time
* join-to-first-chat time for a fresh node
* routing success before/after the relay dies and under churn
* relay load and that relays only ever carried ciphertext

    python3 -I tools/relay_harness.py --nodes 300 --workers 6 --relays 2 --seconds 90
"""
import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def set_bits(bits):
    import mycel.trust as trust
    trust.GLOBAL_BITS = bits


async def relay_main(port, bits):
    set_bits(bits)
    from mycel.relay import Relay, RelayLimits
    r = Relay(relay_id=f"relay-{port}", audit=True, limits=RelayLimits(frames_per_s=2000, burst_frames=6000))
    await r.start("127.0.0.1", port)
    marker = b"harness-secret-prompt"
    while True:
        await asyncio.sleep(2)
        leaked = any(marker in f for f in r.audit_log)
        print(json.dumps({"relay": port, "connected": len(r.conns), **r.stats, "plaintext": leaked}), flush=True)


async def worker_main(wid, count, relays, bits, seconds, seed):
    set_bits(bits)
    from mycel.config import BackendConfig, MeshConfig, NodeConfig
    from mycel.governor import Limits
    from mycel.identity import Identity
    from mycel.node import Node, RouteError
    from mycel.transport import Netem
    from mycel.trust import mint_ticket

    rng = random.Random(seed + wid)
    tmp = tempfile.mkdtemp(prefix=f"harness-{wid}-")
    delay = {}

    def lat(a, b):
        key = (a, b) if a < b else (b, a)
        if key not in delay:
            delay[key] = rng.uniform(0.02, 0.12)
        return delay[key]
    netem = Netem(lat, jitter=0.15, stall_p=0.01, stall_s=0.25)

    def make(i):
        home = [relays[i % len(relays)]] + ([relays[(i + 1) % len(relays)]] if i % 2 == 0 else [])
        serve = i % 4 == 0
        cfg = NodeConfig(name=f"w{wid}n{i}", data_dir=tmp, control_port=0, probe_hardware=False,
                         simulated_hardware={"accelerator": "cuda", "device_name": "sim", "accel_memory_gb": 24.0, "system_memory_gb": 32.0},
                         serve=serve, mesh=MeshConfig(listen=False, relays=home, gossip_interval_s=2.0, advert_ttl_s=20.0),
                         limits=Limits(max_share=1.0, max_concurrent=4, per_peer_per_min=600),
                         backend=BackendConfig(kind="mock", models=["m"], mock_tokens_per_s=400.0))
        n = Node(cfg, Identity.generate())
        n.netem = netem
        n.trust.mesh_id, n.trust.ticket = "global", mint_ticket(n.identity, bits)
        return n

    nodes = [make(i) for i in range(count)]
    for n in nodes:
        await n.start()
        await asyncio.sleep(0.02)
    ok = fail = 0
    t0 = time.monotonic()

    async def traffic():
        nonlocal ok, fail
        while True:
            await asyncio.sleep(0.25)
            src = rng.choice(nodes)
            try:
                async for _ in src.route("m", [{"role": "user", "content": "harness-secret-prompt"}], 8, 0.0,
                                         prefer_local=False, exclude_self=True):
                    pass
                ok += 1
            except RouteError:
                fail += 1

    async def churn():
        while True:
            await asyncio.sleep(rng.uniform(8, 15))
            i = rng.randrange(len(nodes))
            await nodes[i].stop()
            nodes[i] = make(i)
            await nodes[i].start()

    tasks = [asyncio.create_task(traffic()), asyncio.create_task(churn())]
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(2)
        views = [len(n.membership.alive()) for n in nodes]
        print(json.dumps({"worker": wid, "t": round(time.monotonic() - t0, 1), "view_mean": sum(views) / len(views),
                          "view_min": min(views), "ok": ok, "fail": fail}), flush=True)
        ok = fail = 0
    for t in tasks:
        t.cancel()
    for n in nodes:
        await n.stop()


async def fresh_join(relays, bits, total):
    """A brand-new node: how long from start until it has chatted with someone on the mesh?"""
    set_bits(bits)
    from mycel.config import BackendConfig, MeshConfig, NodeConfig
    from mycel.identity import Identity
    from mycel.node import Node, RouteError
    t0 = time.monotonic()
    n = Node(NodeConfig(name="fresh", data_dir=tempfile.mkdtemp(), control_port=0, probe_hardware=False, serve=False,
                        mesh=MeshConfig(listen=False, relays=relays[:1], gossip_interval_s=2.0, advert_ttl_s=20.0),
                        backend=BackendConfig(kind="none")), Identity.generate())
    await n.join_global()
    await n.start()
    first = None
    while time.monotonic() - t0 < 60:
        try:
            async for _ in n.route("m", [{"role": "user", "content": "hi"}], 4, 0.0):
                pass
            first = time.monotonic() - t0
            break
        except RouteError:
            await asyncio.sleep(0.5)
    seen = len(n.membership.alive())
    await n.stop()
    return first, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=300)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--relays", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--bits", type=int, default=12, help="identity-ticket work (real nodes use 22; lowered so 1000 nodes start fast)")
    ap.add_argument("--role", default="coordinator")
    ap.add_argument("--wid", type=int, default=0)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--relay-urls", default="")
    a = ap.parse_args()
    if a.role == "relay":
        return asyncio.run(relay_main(a.port, a.bits))
    if a.role == "worker":
        return asyncio.run(worker_main(a.wid, a.count, a.relay_urls.split(","), a.bits, a.seconds, 7))

    import socket
    ports = []
    for _ in range(a.relays):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); ports.append(s.getsockname()[1]); s.close()
    urls = [f"ws://127.0.0.1:{p}/ws" for p in ports]
    me = [sys.executable, "-I", os.path.abspath(__file__)]
    relays = [subprocess.Popen(me + ["--role", "relay", "--port", str(p), "--bits", str(a.bits)], stdout=subprocess.PIPE, text=True)
              for p in ports]
    time.sleep(1.5)
    per = a.nodes // a.workers
    workers = [subprocess.Popen(me + ["--role", "worker", "--wid", str(w), "--count", str(per), "--relay-urls", ",".join(urls),
                                      "--bits", str(a.bits), "--seconds", str(a.seconds)], stdout=subprocess.PIPE, text=True)
               for w in range(a.workers)]
    import selectors
    sel = selectors.DefaultSelector()
    for p in relays + workers:
        os.set_blocking(p.stdout.fileno(), False)
        sel.register(p.stdout, selectors.EVENT_READ, p)
    t0, killed, joined = time.time(), None, None
    rows, relay_rows, buf = {}, {}, {}
    total = per * a.workers
    print(f"harness: {total} NAT'd nodes in {a.workers} processes, {a.relays} relay processes, WAN latency 20–120 ms, churn on")
    while any(w.poll() is None for w in workers):
        for key, _ in sel.select(timeout=1):
            data = key.fileobj.read() or ""
            buf[key] = buf.get(key, "") + data
            *lines, buf[key] = buf[key].split("\n")
            for line in lines:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "worker" in d:
                    rows.setdefault(d["worker"], []).append(d)
                else:
                    relay_rows[d["relay"]] = d
        el = time.time() - t0
        if joined is None and el > a.seconds * 0.35:
            joined = asyncio.run(fresh_join(urls, a.bits, total))
            print(f"  fresh node: first chat after {joined[0] and round(joined[0], 1)} s, saw {joined[1]} peers")
        if killed is None and el > a.seconds * 0.5:
            relays[0].kill()
            killed = el
            print(f"  t={el:.0f}s  killed relay {ports[0]} (half the nodes had it as their only relay)")
        if rows and int(el) % 10 == 0:
            last = [r[-1] for r in rows.values()]
            ok, fail = sum(r["ok"] for r in last), sum(r["fail"] for r in last)
            print(f"  t={el:5.0f}s  mesh view {sum(r['view_mean'] for r in last) / len(last):6.1f}/{total - 1} avg, "
                  f"min {min(r['view_min'] for r in last)} · routes ok {ok} fail {fail}")
            time.sleep(1)
    for r in relays:
        if r.poll() is None:
            r.terminate()
    def window(lo, hi):
        rs = [x for w in rows.values() for x in w if lo <= x["t"] < hi]
        ok, fail = sum(x["ok"] for x in rs), sum(x["fail"] for x in rs)
        return ok, fail, (ok / max(1, ok + fail))
    before = window(a.seconds * 0.25, a.seconds * 0.5)
    after = window(a.seconds * 0.6, a.seconds)
    print(json.dumps({"nodes": total, "routes_before_relay_kill": before, "routes_after": after,
                      "fresh_join_first_chat_s": joined and joined[0], "relays": relay_rows}, indent=1))


if __name__ == "__main__":
    main()

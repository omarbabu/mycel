"""Command line: run a node, probe hardware, or simulate a whole mesh on one machine."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import tempfile
from concurrent.futures import ProcessPoolExecutor

from . import __version__
from .config import BackendConfig, LocationConfig, MeshConfig, NodeConfig, SwarmConfig, VisibilityConfig, load
from .governor import Limits
from .hardware import MIN_MEMORY_GB, MIN_TOKENS_PER_S, check_serving_eligibility, probe
from .identity import Identity
from .node import Node, RouteError
from .trust import GLOBAL_BITS, GLOBAL_MESH_ID, issue_cert, membership_code, mint_ticket_for

log = logging.getLogger("mycel")


async def _start(port: int, mesh_port: int, data_dir: str, open_browser: bool, seeds: list[str],
                 relays: list[str] | None = None, local: bool = False) -> None:
    """Run your real node: real hardware, real models, your own data folder. Nothing simulated.

    By default the node reaches the mesh through public relays (outbound wss:// only, no open
    port, and only after you join). ``local`` keeps the old single-machine loopback mesh."""
    mesh = (MeshConfig(port=mesh_port, global_bootstrap=seeds) if local else
            MeshConfig(port=mesh_port, global_bootstrap=seeds, listen=False, internet=True, relays=relays or []))
    cfg = NodeConfig(name="default", data_dir=data_dir, control_port=port, mesh=mesh,
                     backend=BackendConfig(kind="managed"))
    node = Node(cfg)
    print("Mycel is starting — measuring your hardware…", flush=True)
    await node.start()
    url = f"http://127.0.0.1:{node.control_port}/"
    hw = node.hw
    print(f"\n  Mycel is running.\n  app        {url}\n  agent API  {url}v1\n"
          f"  device     {hw.device_name} · {hw.accel_memory_gb} GB · {hw.bandwidth_gbs} GB/s · tier {hw.tier}\n"
          f"  data       {node.data_dir}\n  Ctrl-C to stop.\n", flush=True)
    stop = asyncio.Event()
    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):  # stop cleanly (and stop the model server) on kill / Ctrl-C
        loop.add_signal_handler(sig, stop.set)
    if open_browser:
        import subprocess
        import sys as _sys
        opener = "open" if _sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    try:
        await stop.wait()
        print("Stopping Mycel…", flush=True)
    finally:
        await node.stop()


async def _run(cfg_path: str) -> None:
    node = Node(load(cfg_path))
    await node.start()
    print(f"node {node.id}\n  app         http://127.0.0.1:{node.control_port}/\n"
          f"  OpenAI API  http://127.0.0.1:{node.control_port}/v1\n  mesh        {node.advertise_addr}")
    try:
        await asyncio.Event().wait()
    finally:
        await node.stop()


def _probe() -> None:
    hw = probe()
    print(json.dumps(hw.to_dict(), indent=2))
    print(f"\ndetected by: {hw.source} · bandwidth {hw.bandwidth_gbs or '?'} GB/s ({hw.bandwidth_source})")
    print(f"minimum to serve a whole model: a GPU with >= {MIN_MEMORY_GB:.0f} GB, model >= {MIN_TOKENS_PER_S:.0f} tok/s; "
          "swarm slices need >= 4 GB")
    problems = [p for p in check_serving_eligibility(hw, MIN_TOKENS_PER_S) if "backend" not in p]
    print("hardware: ELIGIBLE (backend speed is checked at startup)" if not problems else "hardware: NOT eligible")
    for p in problems:
        print("  -", p)


# ---------------------------------------------------------------- simulation
CITIES = [  # approximate coordinates; nodes snap them to coarse cells anyway. 0 = you (London).
    (51.5, -0.13),
    # Swarm hosts cluster in one region: a chain of stages is only fast between nearby members.
    (52.5, 13.4), (48.9, 2.35), (59.3, 18.1), (52.4, 4.9), (40.4, -3.7), (45.5, 9.2), (52.2, 21.0), (53.3, -6.3), (47.4, 8.5),
    (6.5, 3.4), (-1.3, 36.8), (-23.5, -46.6), (19.1, 72.9), (47.6, -122.3), (35.7, 139.7), (-33.9, 151.2), (19.4, -99.1),
    (30.0, 31.2), (-6.2, 106.8), (43.7, -79.4), (-34.6, -58.4), (1.35, 103.8), (64.1, -21.9), (40.7, -74.0), (37.6, 127.0),
]
# (accel, name, accel GB, whole models served, tok/s, bandwidth GB/s) — tok/s from the catalog estimate
HW_PROFILES = [
    ("mps", "Apple M3 Pro 36GB", 28.1, ["qwen3-30b-a3b"], 43.0, 150.0),
    ("cuda", "RTX 3060 12GB", 12.0, ["qwen3-8b"], 50.0, 360.0),
    ("mps", "Apple M2 16GB", 11.2, ["qwen3-8b"], 14.0, 100.0),
    ("cuda", "RTX 4090 24GB", 24.0, ["gpt-oss-20b"], 300.0, 1008.0),
    ("mps", "Apple M3 Max 64GB", 48.0, ["qwen3-30b-a3b", "gemma-3-27b"], 120.0, 400.0),
    ("cuda", "2x RTX 3090", 48.0, ["llama-3.3-70b"], 15.0, 936.0),
    ("mps", "Apple M2 Ultra 192GB", 140.0, ["gpt-oss-120b"], 140.0, 800.0),
    ("cuda", "GTX 1660 6GB", 6.0, [], 0.0, 192.0),  # below the whole-model minimum: uses the mesh only
]
# Swarm hosts: modest GPUs that each hold a slice of a model none of them could run alone.
SWARM_PROFILES = [
    ("cuda", "RTX 3070 8GB", 8.0, 448.0), ("cuda", "RTX 4060 Ti 16GB", 16.0, 288.0), ("rocm", "RX 6700 XT 12GB", 12.0, 384.0),
    ("mps", "Apple M1 Pro 16GB", 11.2, 200.0), ("cuda", "RTX 2080 8GB", 8.0, 448.0), ("cuda", "RTX 3080 10GB", 10.0, 760.0),
]


def _sim_latency(coords: dict):
    """One-way delay between two simulated nodes: fibre distance plus a little routing overhead."""
    import math

    def one_way(a: str, b: str) -> float:
        pa, pb = coords.get(a), coords.get(b)
        if not pa or not pb:
            return 0.02
        la1, lo1, la2, lo2 = map(math.radians, (*pa, *pb))
        d = 2 * 6371 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
        return (4 + d / 200 * 1.4) / 1000   # light in fibre ~200 km/ms, x1.4 for real-world routes
    return one_way


def _sim_config(i: int, base_mesh: int, base_ctl: int, rng: random.Random, data_dir: str, relays: list[str],
                swarm_model: str | None) -> NodeConfig:
    lat, lon = CITIES[i % len(CITIES)]
    if swarm_model:
        accel, name, gb, bw = SWARM_PROFILES[i % len(SWARM_PROFILES)]
        models, tps = [], 0.0
    else:
        accel, name, gb, models, tps, bw = HW_PROFILES[0 if i == 0 else rng.randrange(1, len(HW_PROFILES))]
    mesh = MeshConfig(port=base_mesh + i, gossip_interval_s=1.5, advert_ttl_s=12.0, listen=not relays, relays=relays)
    return NodeConfig(
        name="sim-you" if i == 0 else f"sim{i}", data_dir=data_dir, control_port=base_ctl + i, probe_hardware=False,
        simulated_hardware={"accelerator": accel, "device_name": name, "accel_memory_gb": gb, "system_memory_gb": max(16.0, gb),
                            "bandwidth_gbs": bw, "bandwidth_source": "table", "source": "simulated"},
        mesh=mesh,
        limits=Limits(max_share=0.25 if i == 0 else rng.choice([0.25, 0.5, 0.75]), max_concurrent=rng.choice([1, 2, 2])),
        location=LocationConfig(share=i != 0 and rng.random() < 0.7, lat=lat, lon=lon,
                                precision=rng.choice(["country", "region", "metro"])),
        # Simulated members make different privacy choices, like real ones would.
        visibility=VisibilityConfig(device=i != 0 and rng.random() < 0.6, activity=i != 0 and rng.random() < 0.5),
        swarm=SwarmConfig(model=swarm_model, budget_gb=round(gb * 0.8, 1) if swarm_model else 0.0, engine="mock", rebalance_s=20.0),
        backend=BackendConfig(kind="mock", models=models or ["none"], mock_tokens_per_s=max(tps, 1.0)),
        serve=bool(models),
    )


def _swarm_plan(n: int) -> dict[int, str]:
    """Which simulated members host swarm slices: enough for Llama 3.3 70B with a replica or two,
    plus a Qwen3 32B swarm that's still assembling (so both states are on screen)."""
    out = {}
    hosts = list(range(1, n))
    for i in hosts[:max(0, min(9, n - 4))]:
        out[i] = "llama-3.3-70b"
    for i in hosts[9:11] if n > 13 else []:
        out[i] = "qwen3-32b"
    return out


async def _simulate(n: int, base_mesh: int, base_ctl: int, traffic: bool, churn: bool, seed: int,
                    private: bool, joined: bool, direct: bool = False, fresh: bool = False) -> None:
    """Run a whole mesh on loopback. Node 0 is *you*: a persistent identity under ~/.mycel,
    so chats, plugins and settings survive restarts.

    Default: members sit behind (simulated) NAT with **no listening ports**, reachable only
    through two local relays, with WAN latency from the distance between their cities. Swarm
    hosts assemble Llama 3.3 70B from slices. ``--direct``: the older all-listening mesh."""
    from .relay import Relay
    from .transport import Netem

    rng = random.Random(seed)
    tmp = tempfile.mkdtemp(prefix="mycel-sim-")
    relays_objs, relay_urls = [], []
    if not direct:
        for region in ("eu-west", "us-east"):
            r = Relay(relay_id=f"relay-{region}", region=region)
            await r.start()
            relays_objs.append(r)
            relay_urls.append(r.url)
    swarm_of = _swarm_plan(n)

    def relays_for(i: int) -> list[str]:
        if direct:
            return []
        lon = CITIES[i % len(CITIES)][1]
        near, far = (relay_urls[1], relay_urls[0]) if lon < -25 else (relay_urls[0], relay_urls[1])
        # Like real nodes: the nearest relay, plus a second one for some (that's what bridges the
        # relays); members on only one relay are reached by visiting it, outbound.
        return [near, far] if i == 0 or i % 3 == 0 else [near]

    cfgs = [_sim_config(i, base_mesh, base_ctl, rng, ("~/.mycel" if not fresh else tmp) if i == 0 else tmp, relays_for(i), swarm_of.get(i))
            for i in range(n)]
    you_name = "sim-private-you" if private else "sim-you"
    cfgs[0].name = you_name
    idents = [None] + [Identity.generate() for _ in range(1, n)]
    you = Node(cfgs[0])
    idents[0] = you.identity
    peers = [f"{idents[i].node_id}@127.0.0.1:{base_mesh + i}" for i in range(1, n)] if direct else []
    coords = {idents[i].node_id: CITIES[i % len(CITIES)] for i in range(n)}
    netem = None if direct else Netem(_sim_latency(coords), jitter=0.15, stall_p=0.01)

    if private:
        if not (you.trust.kind == "private" and you.trust.is_founder):
            you.create_private_mesh()
    else:
        if you.trust.kind == "private":
            you.leave_mesh()  # leftover from a private-mesh demo
        cfgs[0].mesh.global_bootstrap = peers[:4]
        if joined and not you.trust.is_member:
            await you.join_global()
        elif you.trust.is_member:
            you.bootstrap = list(dict.fromkeys(peers[:4] + you.bootstrap))
    # Every member mints its identity ticket in parallel (~1 s of work each); relays require one.
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor() as pool:
        tickets = await asyncio.gather(*(loop.run_in_executor(pool, mint_ticket_for, idents[i].pubkey, GLOBAL_BITS)
                                         for i in range(1, n)))
    you.netem = netem
    await you.start()

    def volunteer(i: int) -> Node:
        node = Node(cfgs[i], idents[i])
        node.netem = netem
        node.trust.ticket = tickets[i - 1]
        if private:
            chain = issue_cert(you.identity, you.trust.chain, node.id, node.identity.pubkey, can_invite=rng.random() < 0.3)
            node.join_private(membership_code(chain, [you.peer_entry()] if direct else []))
        else:
            node.trust.mesh_id = GLOBAL_MESH_ID
            node.bootstrap = [p for j, p in enumerate(peers, 1) if j != i][: 3]
        return node

    nodes: list[Node | None] = [you] + [volunteer(i) for i in range(1, n)]
    for node in nodes[1:]:
        await node.start()
    # Something that keeps knocking without a valid identity. Watch "Refused" on Network.
    intruder = Node(NodeConfig(name="intruder", data_dir=tmp, control_port=0, probe_hardware=False,
                               simulated_hardware=HW_SIM_INTRUDER, mesh=MeshConfig(port=0, gossip_interval_s=3.0,
                                                                                  listen=direct, relays=relay_urls[:1]),
                               backend=BackendConfig(kind="mock", models=["qwen3-8b"])), Identity.generate())
    intruder.trust.mesh_id = GLOBAL_MESH_ID
    intruder.trust.ticket = {"mesh": GLOBAL_MESH_ID, "pubkey": intruder.identity.pubkey, "nonce": 1, "bits": GLOBAL_BITS}
    intruder.bootstrap = peers[:2]
    await intruder.start()
    kind = "private friends mesh (you founded it)" if private else "the global mesh"
    state = "you're in" if you.trust.is_member else "join it from the app"
    net = "direct loopback connections" if direct else f"{len(relays_objs)} local relays, every member behind simulated NAT (no open ports), real-world latency"
    print(f"simulating {kind}: {n - 1} other members + 1 intruder over {net}; "
          f"{sum(1 for v in swarm_of.values() if v == 'llama-3.3-70b')} members assemble Llama 3.3 70B as a swarm; {state}.\n"
          f"  app        http://127.0.0.1:{base_ctl}/\n"
          f"  agent API  http://127.0.0.1:{base_ctl}/v1\n  Ctrl-C to stop.", flush=True)

    async def traffic_loop():
        models = ["qwen3-8b", "gpt-oss-20b", "qwen3-30b-a3b", "llama-3.3-70b", "gpt-oss-120b", "llama-3.3-70b@swarm"]
        while True:
            await asyncio.sleep(rng.uniform(0.6, 2.0))
            live = [x for x in nodes[1:] if x is not None]
            if not live:
                continue
            src = rng.choice(live)

            async def one(src=src):
                try:
                    async for _ in src.route(rng.choice(models), [{"role": "user", "content": f"q{rng.random()}"}],
                                             rng.choice([16, 32, 64]), 0.7, prefer_local=False, exclude_self=True):
                        pass
                except RouteError:
                    pass
            asyncio.create_task(one())

    async def churn_loop():
        while True:
            await asyncio.sleep(rng.uniform(25, 45))
            i = rng.randrange(1, n)
            if nodes[i] is None:
                continue
            await nodes[i].stop()
            nodes[i] = None
            log.info("churn: node %d left", i)
            await asyncio.sleep(rng.uniform(10, 20))
            nodes[i] = volunteer(i)
            await nodes[i].start()
            log.info("churn: node %d rejoined", i)

    tasks = ([asyncio.create_task(traffic_loop())] if traffic else []) + \
            ([asyncio.create_task(churn_loop())] if churn else [])
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await intruder.stop()
        for node in nodes:
            if node is not None:
                await node.stop()
        for r in relays_objs:
            await r.stop()


HW_SIM_INTRUDER = {"accelerator": "cuda", "device_name": "unknown", "accel_memory_gb": 24.0, "system_memory_gb": 32.0}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mycel")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"mycel {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("start", help="run your node (default)")
    st.add_argument("--port", type=int, default=7800, help="app + agent API port (always 127.0.0.1)")
    st.add_argument("--mesh-port", type=int, default=7700)
    st.add_argument("--data-dir", default="~/.mycel")
    st.add_argument("--seed", action="append", default=[], help="global-mesh seed peer <node-id>@<ip>:<port>")
    st.add_argument("--relay", action="append", default=[], help="extra relay to use, wss://host/ws (repeatable)")
    st.add_argument("--local", action="store_true", help="stay on this machine: loopback mesh, no relays")
    st.add_argument("--no-open", action="store_true", help="don't open the app in a browser")
    r = sub.add_parser("run", help="run a node from a TOML config (advanced)")
    r.add_argument("--config", required=True)
    sub.add_parser("probe", help="detect hardware and check the serving minimum")
    rl = sub.add_parser("relay", help="run a relay (forwards end-to-end-encrypted frames; loopback unless --allow-public)")
    rl.add_argument("--host", default="127.0.0.1")
    rl.add_argument("--port", type=int, default=7600)
    rl.add_argument("--region", default="")
    rl.add_argument("--allow-public", action="store_true", help="bind a specific non-loopback address (relay operators only)")
    rl.add_argument("--behind-load-balancer", action="store_true",
                    help="container behind your own TLS load balancer: allows a 0.0.0.0 bind and trusts X-Forwarded-For")
    rl.add_argument("--id", default=None, help="stable relay id shown to nodes (default: random)")
    sd = sub.add_parser("swarm-demo", help="split a real model you've downloaded across N local processes and prove it's exact")
    sd.add_argument("--model", default="qwen3-1.7b")
    sd.add_argument("--gguf", help="path to the model's GGUF (default: find it in ~/.mycel)")
    sd.add_argument("--stages", type=int, default=4)
    sd.add_argument("--tokens", type=int, default=48)
    sd.add_argument("--prompt", default="In two sentences: why do neurons fire together?")
    sd.add_argument("--rtt-ms", type=float, default=0.0, help="inject this round-trip time between nodes")
    sd.add_argument("--heal", action="store_true", help="add a replica per stage and unplug one node mid-reply")
    sd.add_argument("--engine", default="auto", choices=["auto", "torch", "gguf"], help="torch = GPU (fp16), gguf = CPU (NumPy fp32)")
    s = sub.add_parser("simulate", help="run an N-node mesh on loopback with mock models")
    s.add_argument("--nodes", type=int, default=20)
    s.add_argument("--direct", action="store_true", help="old style: every node listens on loopback (no relays)")
    s.add_argument("--fresh", action="store_true", help="'you' starts as a brand-new device (temporary data folder)")
    s.add_argument("--mesh-port", type=int, default=7700)
    s.add_argument("--control-port", type=int, default=7800)
    s.add_argument("--no-traffic", action="store_true")
    s.add_argument("--no-churn", action="store_true")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--private", action="store_true", help="a private friends mesh you founded, instead of the global mesh")
    s.add_argument("--joined", action="store_true", help="start already joined to the global mesh")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd is None:  # plain `mycel` = `mycel start`
        args = ap.parse_args((["-v"] if args.verbose else []) + ["start"])
    try:
        if args.cmd == "start":
            asyncio.run(_start(args.port, args.mesh_port, args.data_dir, not args.no_open, args.seed, args.relay, args.local))
        elif args.cmd == "run":
            asyncio.run(_run(args.config))
        elif args.cmd == "probe":
            _probe()
        elif args.cmd == "relay":
            from .relay import serve
            asyncio.run(serve(args.host, args.port, args.region, args.allow_public, args.behind_load_balancer, args.id))
        elif args.cmd == "swarm-demo":
            from .swarm.demo import run_demo
            asyncio.run(run_demo(args.model, args.gguf, args.stages, args.tokens, args.prompt, args.rtt_ms, args.heal, args.engine))
        else:
            asyncio.run(_simulate(args.nodes, args.mesh_port, args.control_port,
                                  not args.no_traffic, not args.no_churn, args.seed, args.private, args.joined, args.direct, args.fresh))
    except KeyboardInterrupt:
        pass

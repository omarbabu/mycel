"""Spin up N real Mycel nodes on loopback and measure how the protocol behaves as N grows.

Usage: python3 -I tools/scale_test.py <N> [seconds]
Uses cheap 8-bit identity tickets so N nodes start quickly (real nodes use 22 bits).
"""
import asyncio
import json
import random
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mycel.trust as trust  # noqa: E402

trust.GLOBAL_BITS = 8
from mycel.config import BackendConfig, MeshConfig, NodeConfig  # noqa: E402
from mycel.governor import Limits  # noqa: E402
from mycel.identity import Identity  # noqa: E402
from mycel.node import Node, RouteError  # noqa: E402

HW = {"accelerator": "cuda", "device_name": "sim", "accel_memory_gb": 24.0, "tflops_fp16": 20.0, "system_memory_gb": 32.0}


async def main(n: int, run_s: float):
    tmp = tempfile.mkdtemp()
    nodes = []
    for i in range(n):
        serving = i % 10 == 0  # 10% of members share a GPU
        cfg = NodeConfig(name=f"n{i}", data_dir=tmp, control_port=0, probe_hardware=False, serve=serving,
                         simulated_hardware=HW, mesh=MeshConfig(port=0, gossip_interval_s=1.0, advert_ttl_s=30.0),
                         limits=Limits(max_share=1.0, max_concurrent=4, per_peer_per_min=1000),
                         backend=BackendConfig(kind="mock", models=["qwen3-8b"], mock_tokens_per_s=400))
        node = Node(cfg, Identity.generate())
        await node.join_global()
        if nodes:
            node.bootstrap = [x.peer_entry() for x in random.sample(nodes, min(2, len(nodes)))]
        await node.start()
        nodes.append(node)
        if len(nodes) % 25 == 0:
            print(f"started {len(nodes)}", file=sys.stderr, flush=True)
    t0, cpu0 = time.monotonic(), time.process_time()
    target = 0.95 * (n - 1)
    converged_at = None
    while time.monotonic() - t0 < run_s:
        await asyncio.sleep(0.5)
        views = [len(x.membership.alive()) for x in nodes]
        if int((time.monotonic() - t0) * 2) % 10 == 0:
            print(f"t={time.monotonic() - t0:.0f}s view min={min(views)} median={statistics.median(views)}", file=sys.stderr, flush=True)
        if converged_at is None and min(views) >= target:
            converged_at = time.monotonic() - t0
            break
    elapsed = time.monotonic() - t0
    cpu = time.process_time() - cpu0
    views = [len(x.membership.alive()) for x in nodes]
    sizes = [len(json.dumps(x.membership.adverts_for_gossip(x.advert()))) for x in random.sample(nodes, min(20, n))]
    ok = fail = 0
    for x in random.sample(nodes, min(40, n)):
        async def one():
            async for ev in x.route("qwen3-8b", [{"role": "user", "content": "hi"}], 16, 0.7, prefer_local=False, exclude_self=True):
                pass
        try:
            await asyncio.wait_for(one(), 15)
            ok += 1
        except (RouteError, asyncio.TimeoutError):
            fail += 1
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20  # bytes on macOS
    print(json.dumps({
        "nodes": n, "converged_95pct_s": round(converged_at, 1) if converged_at else None,
        "view_min_pct": round(100 * min(views) / (n - 1)), "view_median_pct": round(100 * statistics.median(views) / (n - 1)),
        "gossip_msg_kb_median": round(statistics.median(sizes) / 1024, 1),
        "cpu_s_per_node_per_s": round(cpu / elapsed / n, 4),
        "route_success": f"{ok}/{ok + fail}", "rss_mb_total": round(rss_mb),
    }))
    await asyncio.gather(*(x.stop() for x in nodes), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 60))

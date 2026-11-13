"""``mycel swarm-demo``: split a real model across independent local nodes and prove it's exact.

Every node has its own identity, keys and KV cache, opens no listening port, and talks to the
others only through a relay on loopback — the same sealed protocol the internet mesh uses.
The requester keeps tokenization, embeddings and sampling; the relay's traffic is checked for
plaintext; and the swarm's output is compared token-for-token with a single-process run.
Nothing leaves this machine.
"""
from __future__ import annotations

import asyncio
import glob
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from .. import models as catalog

BOLD, DIM, GREEN, PINK, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[95m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = PINK = RESET = ""


def _find(model_id: str) -> Path | None:
    m = catalog.BY_ID[model_id]
    for p in glob.glob(os.path.expanduser(f"~/.mycel/**/{m.file}"), recursive=True):
        if Path(p).exists():
            return Path(p)
    return None


async def run_demo(model_id: str, gguf: str | None, stages: int, tokens: int, prompt: str, rtt_ms: float = 0.0,
                   heal: bool = False, engine: str = "auto") -> None:
    from ..config import BackendConfig, MeshConfig, NodeConfig, SwarmConfig
    from ..identity import Identity
    from ..node import Node
    from ..relay import Relay
    from ..transport import Netem
    from .engine import LocalModelAny
    from .gguf import GGUF
    from .tokenizer import Tokenizer, render_chat

    m = catalog.BY_ID.get(model_id)
    if m is None or not m.swarm:
        sys.exit(f"{model_id} isn't a swarm-capable model")
    path = Path(gguf).expanduser() if gguf else _find(model_id)
    if not path or not path.exists():
        sys.exit(f"{m.name} isn't on this device. Serve it once in the app (Models) or pass --gguf PATH.")
    per = math.ceil(m.layers / stages)
    if engine == "auto":
        from .engine import torch_device
        engine = "torch" if torch_device() else "gguf"
    eng_label = {"torch": "GPU (PyTorch fp16)", "gguf": "CPU (NumPy fp32)"}[engine]
    print(f"\n{BOLD}Mycel swarm demo{RESET} · {m.name} ({m.layers} layers) split across {stages} nodes, ~{per} layers each · {eng_label}")
    print(f"{DIM}weights: {path}{RESET}")
    tmp = tempfile.mkdtemp(prefix="mycel-swarm-demo-")
    relay = Relay(relay_id="relay-local", region="loopback", audit=True)
    await relay.start()

    def cfg(name: str, host: bool) -> NodeConfig:
        return NodeConfig(name=name, data_dir=tmp, control_port=0, serve=False,
                          mesh=MeshConfig(listen=False, relays=[relay.url], gossip_interval_s=0.3, advert_ttl_s=6.0),
                          swarm=SwarmConfig(model=model_id if host else None, layers=per, budget_gb=1000.0, engine=engine,
                                            files={model_id: str(path)}, rebalance_s=3600, crosscheck=heal, draft=False),
                          backend=BackendConfig(kind="none"))
    n_hosts = stages * (2 if heal else 1)
    t0 = time.time()
    client = Node(cfg("you", False), Identity.generate())
    hosts = [Node(cfg(f"stage{i}", True), Identity.generate()) for i in range(n_hosts)]
    if rtt_ms:
        nem = Netem(lambda a, b: rtt_ms / 2000, jitter=0.1)
        for n in [client, *hosts]:
            n.netem = nem
    for n in [client, *hosts]:
        await n.join_global()
        await n.start()
    print(f"{DIM}{n_hosts} stage nodes + you joined through the relay (no listening ports) — "
          f"each is choosing its own layers…{RESET}")
    for _ in range(600):
        ov = client.swarm_overview().get(model_id)
        if ov and ov["complete"] and ov["hosts"] == n_hosts:
            break
        await asyncio.sleep(0.2)
    else:
        sys.exit("the swarm didn't assemble (not enough memory?)")
    print(f"{GREEN}assembled{RESET} in {time.time() - t0:.1f} s · coverage {''.join('█' if c else '·' for c in ov['coverage'])}")
    for h in sorted(hosts, key=lambda h: h.swarm_host.span):
        s, e = h.swarm_host.span
        print(f"  node {h.id[:6]}  layers {s + 1:>2}–{e:<2}  {h.swarm_host.ms_layer * (e - s):5.1f} ms/token compute")

    msgs = [{"role": "user", "content": prompt}]
    print(f"\n{BOLD}you{RESET}: {prompt}\n{BOLD}{PINK}swarm{RESET}: ", end="", flush=True)
    out, done, killed = [], None, None
    async for ev in client.route(f"{model_id}@swarm", msgs, tokens, 0.0):
        if ev.get("progress") == "route" and heal:
            victim = ev["hops"][len(ev["hops"]) // 2]["node"]
        if "delta" in ev:
            out.append(ev["delta"])
            print(ev["delta"], end="", flush=True)
            if heal and killed is None and len(out) == 8:
                killed = next(h for h in hosts if h.id == victim)
                await killed.stop()
                print(f"{DIM}[✂ node {victim[:6]} unplugged]{RESET}", end="", flush=True)
        elif ev.get("done"):
            done = ev
    text = "".join(out)
    p = done["path"]
    print(f"\n\n{BOLD}path{RESET}: you (tokenize, embed) → " + " → ".join(
        f"{h['node'][:6]} [{h['layers'][0] + 1}–{h['layers'][1]}] {h['compute_ms']:.1f}+{h['net_ms']:.0f} ms" for h in p["hops"]) + " → you (sample)")
    for ev in p["events"]:
        if ev["kind"] == "failover":
            print(f"  {PINK}healed{RESET}: layers {ev['layers'][0] + 1}–{ev['layers'][1]} moved to {', '.join(x[:6] for x in ev['replaced_by'])}, "
                  f"replayed {ev['replayed_tokens']} tokens in {ev['recovery_ms']} ms")
    print(f"  {done['tokens']} tokens · {done['tokens_per_s']} tok/s · first token {done['latency_ms']} ms")
    leaked = any(prompt[:12].encode() in f or text[:10].encode() in f for f in relay.audit_log if len(text) >= 10)
    print(f"  relay forwarded {relay.stats['frames']} frames ({relay.stats['bytes'] / 1e6:.1f} MB) · plaintext seen by relay: "
          f"{'YES' if leaked else GREEN + 'none' + RESET}")

    print(f"\n{DIM}reference: the same model in one process…{RESET}", flush=True)
    g = GGUF(path)
    tok, ref = Tokenizer.from_gguf(g), await asyncio.to_thread(LocalModelAny, g, engine)
    ids = tok.encode(render_chat(m.arch, msgs))
    lg, pos, res = await asyncio.to_thread(ref.step, ids, 0), len(ids), []
    for _ in range(tokens):
        t = int(np.argmax(lg[-1]))
        if t in tok.stop_ids():
            break
        res.append(t)
        lg = await asyncio.to_thread(ref.step, [t], pos)
        pos += 1
    same = tok.decode(res) == text
    print(f"{GREEN + BOLD + '✓ token-for-token identical' if same else 'outputs differ'}{RESET} to running all {m.layers} layers in one place.\n")
    for n in [client, *hosts]:
        if n is not killed:
            await n.stop()
    await relay.stop()

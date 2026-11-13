"""Who hosts which layers, and which chain of nodes a request takes.

**Placement is decentralized.** No coordinator assigns layers. Every hosting node reads the
same gossiped map of who holds what ("stages" in adverts) and picks, for itself, the window of
layers where the swarm is thinnest — first filling holes, then adding replicas where coverage
is lowest. As members join, the model assembles; as more join, every layer gains replicas
(more throughput, painless failover). When someone leaves, the hole shows up in everyone's
map and idle nodes in over-served regions move into it. Moves need hysteresis (only if the
mover's current layers stay covered without it) and are randomized, so nodes don't stampede.

**Routing is per request.** The requester runs a shortest-path search over layer boundaries:
an edge is "node X computes layers a..b", costing its measured round-trip time plus compute
per layer. The cheapest full chain from layer 0 to L wins — which naturally favours few,
nearby, fast hops, and routes around busy or unreliable nodes.
"""
from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import models as catalog

if TYPE_CHECKING:
    from ..node import Node

DEFAULT_RTT_MS = 80.0       # unmeasured peers: assume a typical WAN round trip
HOP_OVERHEAD_MS = 2.0       # sealing, signing and (de)serializing one hop
BOOKENDS_MS = 4.0           # requester-side embedding + LM head per token


@dataclass
class Span:
    node: str
    model: str
    start: int
    end: int
    ms_layer: float = 5.0
    rtt_ms: float = 0.0
    free: int = 1
    reliability: float = 1.0
    hint: dict = field(default_factory=dict)
    local: bool = False

    def cost(self, a: int, b: int) -> float:
        rtt = 0.0 if self.local else (self.rtt_ms or DEFAULT_RTT_MS)
        return (rtt + HOP_OVERHEAD_MS + self.ms_layer * (b - a)) / max(0.05, self.reliability)


def spans_for(node: "Node", model: str, exclude_self: bool = False, include_loading: bool = False) -> list[Span]:
    """Slices of ``model`` on the mesh. Routing uses ready slices; placement also counts claims
    from slices still loading."""
    out: list[Span] = []
    host = node.swarm_host
    if not exclude_self and host.ready and host.model_id == model:
        s, e = host.span
        out.append(Span(node.id, model, s, e, host.ms_layer, 0.0, host.free_sessions, 1.0, {}, True))
    for p in node.membership.alive():
        b = p.body
        if node._transport_for(b) is None:
            continue
        if p.ok + p.failed >= 5 and p.reliability < 0.4:
            continue
        for st in b.get("stages") or []:
            if st.get("m") != model or (st.get("loading") and not include_loading):
                continue
            out.append(Span(p.node_id, model, int(st["s"]), int(st["e"]), float(st.get("ms") or 5.0), p.rtt_ms,
                            int(st.get("free", 1)), p.reliability, b))
    return out


def coverage(spans: list[Span], n_layers: int) -> list[int]:
    cov = [0] * n_layers
    for s in spans:
        for i in range(max(0, s.start), min(n_layers, s.end)):
            cov[i] += 1
    return cov


def plan(spans: list[Span], n_layers: int, lo: int = 0, hi: int | None = None,
         exclude: set[str] | frozenset = frozenset()) -> list[tuple[Span, int, int]] | None:
    """Cheapest chain of (span, a, b) covering layers [lo, hi). None if there's a gap."""
    hi = n_layers if hi is None else hi
    usable = [s for s in spans if s.node not in exclude and s.free > 0 and s.end > lo and s.start < hi]
    points = sorted({lo, hi} | {min(hi, max(lo, s.start)) for s in usable} | {min(hi, max(lo, s.end)) for s in usable})
    best = {lo: 0.0}
    prev: dict[int, tuple[int, Span]] = {}
    heap = [(0.0, lo)]
    while heap:
        d, p = heapq.heappop(heap)
        if p == hi:
            break
        if d > best.get(p, float("inf")):
            continue
        for s in usable:
            if not s.start <= p < s.end:
                continue
            for q in points:
                if p < q <= min(s.end, hi):
                    nd = d + s.cost(p, q)
                    if nd < best.get(q, float("inf")) - 1e-9:
                        best[q] = nd
                        prev[q] = (p, s)
                        heapq.heappush(heap, (nd, q))
    if hi not in best:
        return None
    chain, q = [], hi
    while q != lo:
        p, s = prev[q]
        chain.append((s, p, q))
        q = p
    return chain[::-1]


def chain_ms(chain: list[tuple[Span, int, int]]) -> float:
    return sum(s.cost(a, b) for s, a, b in chain) + BOOKENDS_MS


def _pref(my_id: str, n: int) -> int:
    return int.from_bytes(hashlib.sha256(my_id.encode()).digest()[:4], "big") % max(1, n)


def choose_span(others: list[Span], n_layers: int, k: int, my_id: str) -> tuple[int, int]:
    """The window of k layers this node should host: thinnest coverage first, aligned with
    neighbours' boundaries (so chains need fewer hops), ties spread by node id."""
    if k >= n_layers:
        return 0, n_layers
    cov = coverage(others, n_layers)
    edges = {0, n_layers - k} | {s.end for s in others if s.end <= n_layers - k} | {s.start - k for s in others if s.start - k >= 0}
    pref = _pref(my_id, n_layers - k + 1)

    def score(st: int):
        w = cov[st:st + k]
        return (min(w), sum(w), st not in edges, abs(st - pref))
    st = min(range(n_layers - k + 1), key=score)
    return st, st + k


def _move_target(spans: list[Span], me: Span, n_layers: int) -> tuple[int, int] | None:
    """Where ``me`` would move given everyone else's slices, or None if it should stay: it may
    leave only if its layers stay covered without it, for a strictly thinner window."""
    k = me.end - me.start
    rest = [o for o in spans if o is not me]
    cov = coverage(rest, n_layers)
    without_me = min(cov[me.start:me.end])
    if without_me < 1 or k >= n_layers:
        return None
    cand = choose_span(rest, n_layers, k, me.node)
    if cand != (me.start, me.end) and min(cov[cand[0]:cand[1]]) < without_me:
        return cand
    return None


def better_span(others: list[Span], n_layers: int, k: int, my_id: str, current: tuple[int, int]) -> tuple[int, int] | None:
    """Should this node move? Only if its layers stay covered without it and some window is
    strictly thinner — and only if no *other node that could also move* and overlaps its layers
    has a lower id. Overlapping movers therefore never leave at the same moment, which keeps
    decentralized re-placement from opening a gap, without ever deadlocking."""
    me = Span(my_id, "", current[0], current[1])
    everyone = others + [me]
    target = _move_target(everyone, me, n_layers)
    if target is None:
        return None
    for o in others:
        if o.start < me.end and o.end > me.start and o.node < my_id and _move_target(everyone, o, n_layers):
            return None
    return target


def missing_ranges(cov: list[int]) -> list[tuple[int, int]]:
    out, start = [], None
    for i, c in enumerate(cov + [1]):
        if c == 0 and start is None:
            start = i
        elif c and start is not None:
            out.append((start, i))
            start = None
    return out


def overview(node: "Node") -> dict[str, dict]:
    """Every swarm on the mesh: coverage per layer, hosts, gaps and the speed a request would get."""
    models: dict[str, list[Span]] = {}
    for m in catalog.CATALOG:
        if m.swarm:
            spans = spans_for(node, m.id)
            if spans:
                models[m.id] = spans
    out = {}
    for mid, spans in models.items():
        m = catalog.BY_ID[mid]
        L = m.layers
        cov = coverage(spans, L)
        chain = plan(spans, L)
        gaps = missing_ranges(cov)
        missing = sum(b - a for a, b in gaps)
        ms = chain_ms(chain) if chain else None
        out[mid] = {
            "layers": L, "coverage": cov, "covered": round(1 - missing / L, 3), "complete": chain is not None,
            "hosts": len({s.node for s in spans}), "min_replicas": min(cov), "gaps": gaps,
            "need_gb": round(missing * m.layer_gb, 1), "hops": len(chain) if chain else None,
            "est_ms_per_token": round(ms, 1) if ms else None, "est_tps": round(1000 / ms, 1) if ms else None,
            "free_sessions": sum(s.free for s in spans),
        }
    return out

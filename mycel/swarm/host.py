"""Hosting a slice of a big model: this node's part of a swarm.

The owner picks *which model* to help with (or accepts the suggestion); Mycel picks *which
layers*, using the gossiped coverage map (see ``planner``), and moves the slice when the swarm
needs it elsewhere. Stage work counts against the same hard GPU-share limit as everything else
— charged per forward pass, by compute actually spent.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

import numpy as np

from .. import models as catalog
from ..identity import b64d, b64e
from . import planner
from .engine import MOCK_DIM, MockStage, make_stage, torch_device

if TYPE_CHECKING:
    from ..node import Node

log = logging.getLogger("mycel")
SESSION_IDLE_S = 120.0
MAX_SESSIONS_PER_PEER = 2
KV_RESERVE_GB = 0.75
MIN_SWARM_MEMORY_GB = 4.0


@dataclass
class _Session:
    a: int
    b: int
    cache: KVCache
    token: str = ""
    last: float = field(default_factory=time.monotonic)


def weights_session():
    """HTTP session for weight downloads: certificate verification always on (certifi CA bundle)."""
    import ssl

    import aiohttp
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx), timeout=aiohttp.ClientTimeout(total=None, sock_read=120))


def _fail(status: int, msg: str):
    from ..node import _Fail
    return _Fail(status, msg)


class SwarmHost:
    def __init__(self, node: "Node"):
        self.node = node
        self.model_id: str | None = None
        self.span: tuple[int, int] | None = None
        self.engine = None
        self.status = "idle"            # idle | waiting | downloading | loading | ready | error
        self.progress: float | None = None
        self.error: str | None = None
        self.ms_layer = 0.0
        self.sessions: dict[tuple[str, str], _Session] = {}
        self.byzantine = False          # tests/simulations only: a node that returns wrong activations
        self.history: deque = deque(maxlen=12)
        self.stats = {"forwards": 0, "tokens": 0, "compute_ms": 0.0, "moves": 0, "peers": set()}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ state
    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.engine is not None and self.span is not None

    @property
    def hosting(self) -> bool:
        return self.model_id is not None

    @property
    def free_sessions(self) -> int:
        return max(0, self.node.cfg.swarm.max_sessions - len(self.sessions))

    def engine_kind(self) -> str:
        k = self.node.cfg.swarm.engine
        if k != "auto":
            return k
        if self.node.cfg.backend.kind == "mock":
            return "mock"
        return "torch" if torch_device() else "gguf"

    def max_layers(self, model_id: str, ignore_active: bool = False) -> int:
        """How many layers of this model fit in the memory this node can give a slice."""
        m = catalog.BY_ID[model_id]
        hw = self.node.hw
        if not m.swarm or hw is None or not hw.is_gpu:
            return 0
        budget = self.node.cfg.swarm.budget_gb
        if not budget:
            used = 0.0
            if self.node.serving and self.node.backend and not ignore_active:
                served = catalog.BY_ID.get((self.node.backend.models or [""])[0])
                used = served.memory_gb if served else 0.0
            budget = hw.accel_memory_gb * 0.8 - used - KV_RESERVE_GB
        per = m.layer_gb_for(self.engine_kind())
        return max(0, min(m.layers, int(budget / per))) if per else 0

    def layer_capacity(self, model_id: str, ignore_active: bool = False) -> int:
        """Layers this node will host: the owner's chosen count, capped by what fits."""
        fit = self.max_layers(model_id, ignore_active)
        want = self.node.cfg.swarm.layers
        return min(fit, want) if want else fit

    def problems(self, model_id: str) -> list[str]:
        m = catalog.BY_ID.get(model_id)
        hw = self.node.hw
        if m is None or not m.swarm:
            return ["this model can't be split into a swarm yet"]
        if hw is None or not hw.is_gpu:
            return ["swarm hosting needs a GPU (Apple Metal, CUDA, ROCm or Vulkan)"]
        if hw.accel_memory_gb < MIN_SWARM_MEMORY_GB:
            return [f"needs at least {MIN_SWARM_MEMORY_GB:.0f} GB of GPU memory"]
        if self.layer_capacity(model_id) < 1:
            if self.node.serving and self.node.backend and self.node.backend.models and \
                    self.max_layers(model_id, ignore_active=True) >= 1:
                return [f"your GPU memory is in use by {catalog.BY_ID.get(self.node.backend.models[0], m).name} — "
                        "stop serving it (or pick a smaller model) to host a slice"]
            return ["not enough free GPU memory for even one layer"]
        return []

    def advert_entries(self) -> list[dict]:
        """Our slice as others see it. A slice still loading is advertised too (as a claim), so
        nodes placing themselves at the same moment don't all pick the same layers."""
        if self.span is None or self.status not in ("loading", "ready"):
            return []
        s, e = self.span
        entry = {"m": self.model_id, "s": s, "e": e, "ms": round(self.ms_layer, 3), "free": self.free_sessions}
        if not self.ready:
            entry["loading"] = True
        return [entry]

    def snapshot(self) -> dict:
        m = catalog.BY_ID.get(self.model_id) if self.model_id else None
        return {"model": self.model_id, "status": self.status, "error": self.error, "progress": self.progress,
                "chosen_layers": self.node.cfg.swarm.layers,
                "span": list(self.span) if self.span else None, "layers": m.layers if m else None,
                "capacity": self.layer_capacity(self.model_id) if self.model_id else None,
                "ms_per_layer": round(self.ms_layer, 3), "sessions": len(self.sessions),
                "engine": self.engine_kind(), "history": list(self.history),
                "forwards": self.stats["forwards"], "tokens": self.stats["tokens"],
                "people": len(self.stats["peers"]), "moves": self.stats["moves"]}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Resume a saved slice. If it no longer fits (say the owner's model came back too), report
        it in the UI instead of failing the whole node."""
        if self.node.cfg.swarm.model:
            try:
                await self.set_model(self.node.cfg.swarm.model, persist=False)
            except ValueError as exc:
                self.model_id, self.status, self.error = self.node.cfg.swarm.model, "error", str(exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.engine, self.sessions, self.span = None, {}, None

    async def set_model(self, model_id: str | None, persist: bool = True, layers: int | None = None) -> None:
        if layers is not None:
            self.node.cfg.swarm.layers = max(0, int(layers))
        if model_id is not None:
            problems = self.problems(model_id)
            if problems:
                raise ValueError(problems[0])
        await self.stop()
        self.model_id, self.status, self.error = model_id, "idle" if model_id is None else "waiting", None
        self.node.cfg.swarm.model = model_id
        if persist:
            self.node.save_settings()
        if model_id is not None:
            self._task = asyncio.create_task(self._run(model_id))

    def _others(self, model_id: str) -> list[planner.Span]:
        return planner.spans_for(self.node, model_id, exclude_self=True, include_loading=True)

    async def _run(self, model_id: str) -> None:
        m = catalog.BY_ID[model_id]
        try:
            # Let a few gossip rounds arrive so placement sees the current swarm.
            await asyncio.sleep(min(6.0, 3 * self.node.cfg.mesh.gossip_interval_s) * random.uniform(0.6, 1.4))
            k = self.layer_capacity(model_id)
            span = planner.choose_span(self._others(model_id), m.layers, k, self.node.id)
            await self._load(model_id, span, "filled the thinnest layers" if k < m.layers else "holds the whole model")
            checks = 0
            while True:
                # Settle quickly at first (others may have placed themselves at the same moment),
                # then re-check at the configured, much slower pace.
                settle = max(1.0, 4 * self.node.cfg.mesh.gossip_interval_s)
                await asyncio.sleep((settle if checks < 8 else self.node.cfg.swarm.rebalance_s) * random.uniform(0.8, 1.2))
                checks += 1
                self._gc()
                if self.sessions:
                    continue  # never move while computing for someone
                cand = planner.better_span(self._others(model_id), m.layers, k, self.node.id, self.span)
                if cand:
                    self.stats["moves"] += 1
                    await self._load(model_id, cand, "moved to where the swarm was thinner")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status, self.error = "error", str(exc)
            log.warning("swarm hosting %s failed: %s", model_id, exc)

    async def _load(self, model_id: str, span: tuple[int, int], why: str) -> None:
        """Load a slice. When moving, the old slice keeps serving until the new one is ready."""
        moving = self.ready
        if not moving:
            self.status, self.span = "loading", span
            self.sessions = {}
        engine, ms = await self._make_engine(model_id, span)
        self.engine, self.span, self.ms_layer, self.status = engine, span, ms, "ready"
        if moving:
            self.sessions = {}   # sessions on the old layers end; requesters re-plan (409)
        self.history.appendleft({"at": time.time(), "span": list(span), "why": why})
        log.info("swarm: hosting %s layers %d-%d (%s)", model_id, span[0], span[1], why)

    async def _make_engine(self, model_id: str, span: tuple[int, int]):
        m = catalog.BY_ID[model_id]
        s, e = span
        kind = self.engine_kind()
        if kind == "mock":
            ms = m.est_layer_ms(self.node.hw.bandwidth_gbs if self.node.hw else 0.0) or 4.0
            return MockStage(m.layers, s, e, ms, self.byzantine), ms
        path = self.weights_path(model_id) or self.slice_path(model_id, s, e)
        if not path.exists():
            path = await self._download(model_id, s, e, path)
        self.status = "loading"

        def build():
            from .gguf import GGUF
            st = make_stage(kind, GGUF(path), s, e)
            cache = st.new_cache()
            x = np.zeros((1, st.cfg.d_model), np.float32)
            st.forward(x, 0, cache)
            st.forward(x, 1, cache)
            t = time.perf_counter()
            st.forward(x, 2, cache)
            return st, (time.perf_counter() - t) * 1000 / (e - s)
        return await asyncio.to_thread(build)

    def slice_path(self, model_id: str, s: int, e: int) -> Path:
        return self.node.data_dir / "slices" / f"{model_id}-{s}-{e}.gguf"

    async def _download(self, model_id: str, s: int, e: int, dest: Path, bookends: bool = False, track: bool = True) -> Path:
        """Fetch only this slice's tensors (HTTP range requests) from the model's published GGUF."""
        from .gguf import fetch_slice
        m = catalog.BY_ID[model_id]
        if track:
            self.status, self.progress = "downloading", 0.0
        url = f"{self.node.cfg.swarm.weights_url.rstrip('/')}/{m.hf.split(':')[0]}/resolve/main/{m.file}"
        async with weights_session() as session:
            out = await fetch_slice(session, url, dest, s, e, bookends=bookends,
                                    progress=(lambda f: setattr(self, "progress", round(f, 3))) if track else None)
        if track:
            self.progress = None
        return out

    def weights_path(self, model_id: str) -> Path | None:
        """A local GGUF holding this model: configured file, or one llama-server already downloaded."""
        m = catalog.BY_ID[model_id]
        p = self.node.cfg.swarm.files.get(model_id)
        if p and Path(p).expanduser().exists():
            return Path(p).expanduser()
        d = self.node.models.models_dir
        if d.exists():
            for f in d.rglob(f"*{m.file}"):
                if f.exists():
                    return f
        return None

    def _gc(self) -> None:
        now = time.monotonic()
        for k in [k for k, s in self.sessions.items() if now - s.last > SESSION_IDLE_S]:
            del self.sessions[k]

    # ------------------------------------------------------------------ computing
    async def forward(self, sender: str, sid: str, model: str, a: int, b: int, pos: int, x: np.ndarray,
                      token: str = "", relayed: bool = False) -> tuple[np.ndarray, float]:
        """Run layers [a, b) for one session. Raises the node's _Fail with an HTTP-like status.
        ``relayed``: the activations arrived from the previous stage (express lane) on behalf of
        ``sender``; such requests must present the session's token and can't open sessions."""
        if not self.ready or model != self.model_id:
            raise _fail(404, "not hosting that model right now")
        s, e = self.span
        if not s <= a < b <= e:
            raise _fail(409, "those layers moved; re-plan")
        T, D = x.shape
        want_d = MOCK_DIM if isinstance(self.engine, MockStage) else self.engine.cfg.d_model
        if D != want_d or not 0 < T <= 4096:
            raise _fail(400, "activation shape doesn't match this model")
        est = self.ms_layer * (b - a) * (1 + 0.04 * (T - 1)) / 1000
        if sender != self.node.id and not self.node.governor.can_spend(est):
            raise _fail(429, "compute budget exhausted; retry later")
        key = (sender, sid)
        sess = self.sessions.get(key)
        if sess is None and relayed:
            raise _fail(409, "unknown session for an express-lane hop")
        if sess is None:
            self._gc()
            if len(self.sessions) >= self.node.cfg.swarm.max_sessions:
                raise _fail(429, "this stage is at its session limit")
            if sum(1 for (p, _) in self.sessions if p == sender) >= MAX_SESSIONS_PER_PEER:
                raise _fail(429, "too many sessions from one requester")
            if pos != 0:
                raise _fail(409, "unknown session: replay from position 0")
            sess = self.sessions[key] = _Session(a, b, self.engine.new_cache(), token)
        if sess.token and token != sess.token:
            raise _fail(403, "session token mismatch")
        if (sess.a, sess.b) != (a, b):
            raise _fail(400, "a session always covers the same layers")
        if pos > sess.cache.length:
            raise _fail(409, "gap in context: replay from position 0")
        sess.last = time.monotonic()
        t0 = time.perf_counter()
        if isinstance(self.engine, MockStage):
            out = self.engine.forward(x, pos, sess.cache, a, b)
            await asyncio.sleep(self.engine.compute_seconds(T, b - a))
        else:
            out = await asyncio.to_thread(self.engine.forward, x, pos, sess.cache, a, b)
        ms = (time.perf_counter() - t0) * 1000
        if sender != self.node.id:
            self.node.governor.charge(ms / 1000)
            self.stats["peers"].add(sender)
            if T == 1:
                self.node.ledger.record("given", 1, sender)
        self.stats["forwards"] += 1
        self.stats["tokens"] += T
        self.stats["compute_ms"] += ms
        self.node.counters["stage_forwards"] += 1
        return out, ms

    async def rpc_forward(self, sender: str, body: dict) -> AsyncIterator[dict]:
        T, D = (int(v) for v in body["shape"])
        raw = b64d(body["x"])
        if len(raw) != T * D * 4:
            raise _fail(400, "activation size mismatch")
        x = np.frombuffer(raw, dtype=np.float32).reshape(T, D)
        origin = str(body.get("for") or sender)[:64]
        relayed = origin != sender
        if relayed and origin not in self.node.membership.peers and origin != self.node.id:
            raise _fail(403, "unknown requester")
        out, ms = await self.forward(origin, str(body["session"])[:64], str(body["model"]), int(body["start"]),
                                     int(body["end"]), int(body["pos"]), x, str(body.get("token", ""))[:64], relayed)
        enc = b64e(np.ascontiguousarray(out, np.float32).tobytes())
        reply = body.get("reply")
        if isinstance(reply, dict):
            # Express lane: copy our output back to the requester and hand it straight to the next stage.
            asyncio.create_task(self._express(origin, body, reply, out.shape, enc, ms))
            yield {"queued": True, "compute_ms": round(ms, 3)}
            return
        yield {"x": enc, "shape": list(out.shape), "compute_ms": round(ms, 3)}

    async def _express(self, origin: str, body: dict, reply: dict, shape, enc: str, ms: float) -> None:
        from ..node import RouteError

        def hint(nid):
            rec = self.node.membership.peers.get(nid)
            return rec.body if rec else None
        idx, tag = int(reply.get("idx", 0)), str(reply.get("tag", ""))[:32]
        chain = body.get("chain") or []

        async def push(item: dict) -> None:
            h = hint(origin)
            if h is None:
                return
            try:
                await self.node.rpc_one(origin, h, "stage.result", {"tag": tag, **item}, timeout=10)
            except RouteError:
                pass
        tasks = [asyncio.create_task(push({"idx": idx, "x": enc, "shape": list(shape), "compute_ms": round(ms, 3)}))]
        if chain and isinstance(chain, list) and len(chain) <= 64:
            nxt = chain[0]
            h = hint(str(nxt.get("node")))
            try:
                if h is None:
                    raise RouteError("next stage unknown here")
                await self.node.rpc_one(str(nxt["node"]), h, "stage.forward", {
                    "session": nxt["session"], "token": nxt.get("token", ""), "model": body["model"], "start": nxt["start"],
                    "end": nxt["end"], "pos": body["pos"], "shape": list(shape), "x": enc, "for": origin,
                    "chain": chain[1:], "reply": {"tag": tag, "idx": idx + 1}}, timeout=15)
            except (RouteError, KeyError, TypeError, ValueError) as exc:
                tasks.append(asyncio.create_task(push({"idx": idx + 1, "error": str(exc)[:160], "node": str(nxt.get("node", ""))})))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def rpc_close(self, sender: str, body: dict) -> AsyncIterator[dict]:
        self.sessions.pop((sender, str(body.get("session", ""))[:64]), None)
        yield {"ok": True}

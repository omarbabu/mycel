"""Running a reply through a swarm: plan a chain of stages, stream activations through it,
survive stages vanishing mid-reply, cross-check one stage against a replica, and speculate.

The requester keeps the *bookends*: it tokenizes, embeds, applies the LM head and samples, so
no node in the chain ever handles text — only activations (which a determined node could
still try to invert, so this is "no node sees your words directly", not cryptographic privacy).

Every hop is an end-to-end-sealed RPC; the trace of the trip (who computed which layers, how
long compute and network took, failovers, cross-checks, speculation) is returned with the
reply so the app can show exactly what happened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import time
from typing import TYPE_CHECKING, AsyncIterator

import numpy as np

from .. import models as catalog
from ..identity import b64d, b64e
from . import planner
from .engine import MOCK_DIM, MockBookends

if TYPE_CHECKING:
    from ..node import Node

log = logging.getLogger("mycel")
MAX_PAYLOAD = 768 << 10          # bytes of activations per hop call (prefill is chunked)
SPEC_K = 4                       # draft tokens proposed per speculative round
MOCK_ACCEPT = 0.7                # simulated swarms: probability a draft token is accepted


def _route_error(msg: str):
    from ..node import RouteError
    return RouteError(msg)


class _Hop:
    def __init__(self, span: planner.Span, a: int, b: int):
        self.span, self.a, self.b = span, a, b
        self.sid = secrets.token_hex(8)
        self.token = secrets.token_hex(16)   # lets the previous stage feed this session (express lane)
        self.inputs: np.ndarray | None = None
        self.prefill_out: np.ndarray | None = None
        self.calls = 0
        self.compute_ms = 0.0
        self.net_ms = 0.0

    def record_input(self, pos: int, x: np.ndarray) -> None:
        prev = self.inputs[:pos] if self.inputs is not None else np.empty((0, x.shape[1]), np.float32)
        self.inputs = np.concatenate([prev, x], axis=0)


class Pipeline:
    def __init__(self, node: "Node", model: catalog.CatalogModel, chain, exclude_self: bool, progress: asyncio.Queue | None):
        self.node, self.m, self.exclude_self = node, model, exclude_self
        self.hops = [_Hop(s, a, b) for s, a, b in chain]
        self.excluded: set[str] = set()
        self.events: list[dict] = []
        self.t0 = time.monotonic()
        self.progress = progress
        self.closed: list[_Hop] = []
        self.express_steps = 0
        self.express_fallbacks = 0
        self.refreshed: np.ndarray | None = None   # new prefill output if a cross-check replaced a liar

    def _emit(self, ev: dict) -> None:
        if self.progress is not None:
            self.progress.put_nowait(ev)

    def describe(self) -> list[dict]:
        return [{"node": h.span.node, "layers": [h.a, h.b], "local": h.span.local} for h in self.hops]

    async def _call(self, hop: _Hop, pos: int, x: np.ndarray) -> np.ndarray:
        T, D = x.shape
        rows = max(1, MAX_PAYLOAD // (D * 4))
        outs = []
        for off in range(0, T, rows):
            chunk = np.ascontiguousarray(x[off:off + rows], np.float32)
            t = time.perf_counter()
            if hop.span.local:
                out, ms = await self.node.swarm_host.forward(self.node.id, hop.sid, self.m.id, hop.a, hop.b, pos + off, chunk, hop.token)
            else:
                item = await self.node.rpc_one(hop.span.node, hop.span.hint, "stage.forward", {
                    "session": hop.sid, "token": hop.token, "model": self.m.id, "start": hop.a, "end": hop.b, "pos": pos + off,
                    "shape": list(chunk.shape), "x": b64e(chunk.tobytes())}, timeout=30)
                shape = tuple(int(v) for v in item["shape"])
                out = np.frombuffer(b64d(item["x"]), np.float32).reshape(shape)
                ms = float(item.get("compute_ms", 0.0))
                if shape != chunk.shape or not np.isfinite(out).all():
                    raise _route_error("stage returned malformed activations")
            hop.net_ms += max(0.0, (time.perf_counter() - t) * 1000 - ms)
            hop.compute_ms += ms
            hop.calls += 1
            outs.append(out)
        return outs[0] if len(outs) == 1 else np.concatenate(outs, axis=0)

    async def forward(self, x: np.ndarray, pos: int, prefill: bool = False) -> np.ndarray:
        """Push T new positions through every hop. Stages truncate their cache to ``pos`` first,
        which is also how rejected speculative tokens are rolled back."""
        from ..node import RouteError

        i = 0
        while i < len(self.hops):
            hop = self.hops[i]
            hop.record_input(pos, x)
            try:
                x = await self._call(hop, pos, x)
                if prefill:
                    hop.prefill_out = x
                    self._emit({"progress": "prefill", "hop": i, "of": len(self.hops), "node": hop.span.node, "layers": [hop.a, hop.b]})
                i += 1
            except RouteError as exc:
                x, added = await self._failover(i, exc, pos, x.shape[0])
                if prefill:
                    for j in range(i, i + added):
                        self.hops[j].prefill_out = None
                i += added
        return x

    async def forward_express(self, x: np.ndarray, pos: int) -> np.ndarray:
        """One decode step down the express lane: hop 0 computes and hands its output straight to
        hop 1, and so on; every stage also copies its output back to us in parallel (we keep those
        for replay and cross-checks). N+1 network legs per token instead of 2N. Any hiccup falls
        back to the orchestrated path for this step, which handles failover."""
        from ..node import RouteError

        hops = self.hops
        if len(hops) < 2 or any(h.span.local for h in hops) or x.shape[0] * x.shape[1] * 4 > MAX_PAYLOAD:
            return await self.forward(x, pos)
        client = self.node.swarm_client
        tag = secrets.token_hex(8)
        q = client.expect(tag, [h.span.node for h in hops])
        first = hops[0]
        t0 = time.perf_counter()
        try:
            await self.node.rpc_one(first.span.node, first.span.hint, "stage.forward", {
                "session": first.sid, "token": first.token, "model": self.m.id, "start": first.a, "end": first.b,
                "pos": pos, "shape": list(x.shape), "x": b64e(np.ascontiguousarray(x, np.float32).tobytes()),
                "chain": [{"node": h.span.node, "session": h.sid, "token": h.token, "start": h.a, "end": h.b} for h in hops[1:]],
                "reply": {"tag": tag, "idx": 0}}, timeout=15)
            outs: dict[int, tuple] = {}
            budget = max(1.5, 4 * planner.chain_ms([(h.span, h.a, h.b) for h in hops]) / 1000)
            while len(outs) < len(hops):
                item = await asyncio.wait_for(q.get(), budget)
                if "error" in item:
                    raise RouteError(item["error"])
                outs[int(item["idx"])] = (np.frombuffer(b64d(item["x"]), np.float32).reshape(x.shape), float(item.get("compute_ms", 0)))
        except (RouteError, asyncio.TimeoutError, KeyError, ValueError):
            self.express_fallbacks += 1
            return await self.forward(x, pos)
        finally:
            client.forget(tag)
        total = (time.perf_counter() - t0) * 1000
        first.record_input(pos, x)
        compute = sum(ms for _, ms in outs.values())
        for i, h in enumerate(hops):
            if i:
                h.record_input(pos, outs[i - 1][0])
            h.compute_ms += outs[i][1]
            h.net_ms += max(0.0, total - compute) / len(hops)
            h.calls += 1
        self.express_steps += 1
        return outs[len(hops) - 1][0]

    async def _failover(self, i: int, exc: Exception, pos: int, T: int) -> tuple[np.ndarray, int]:
        from ..node import RemoteError, RouteError

        bad = self.hops[i]
        moved = isinstance(exc, RemoteError) and exc.status in (404, 409)   # re-placed, not broken
        if not moved:
            self.excluded.add(bad.span.node)
            rec = self.node.membership.peers.get(bad.span.node)
            if rec is not None:
                rec.failed += 1
        t0 = time.monotonic()
        skip = set(self.excluded) | {bad.span.node}
        wait = max(0.3, 2 * self.node.cfg.mesh.gossip_interval_s)   # time for fresh adverts to arrive
        for attempt in range(6):
            spans = planner.spans_for(self.node, self.m.id, self.exclude_self)
            sub = planner.plan(spans, self.m.layers, bad.a, bad.b, exclude=skip)
            if not sub and moved:   # the mover may now hold different layers: let gossip catch up
                sub = planner.plan(spans, self.m.layers, bad.a, bad.b, exclude=self.excluded)
            if not sub:
                if attempt < 5:
                    await asyncio.sleep(wait)
                    continue
                raise RouteError(f"layers {bad.a + 1}–{bad.b} of {self.m.name} lost their host and no replica is free")
            new = [_Hop(s, a, b) for s, a, b in sub]
            inp = bad.inputs
            try:
                for h in new:   # replay everything so far to rebuild the replacement's KV cache
                    h.inputs = inp
                    inp = await self._call(h, 0, inp)
            except RouteError as again:
                if isinstance(again, RemoteError) and again.status in (404, 409):
                    await asyncio.sleep(wait)   # stale map: someone else moved too
                else:
                    self.excluded.update(h.span.node for h in new if not h.span.local)
                    skip.update(h.span.node for h in new)
                continue
            self.hops[i:i + 1] = new
            self.closed.append(bad)
            ev = {"kind": "moved" if moved else "failover", "at_ms": round((time.monotonic() - self.t0) * 1000),
                  "node": bad.span.node, "layers": [bad.a, bad.b], "replaced_by": [h.span.node for h in new],
                  "replayed_tokens": int(bad.inputs.shape[0]), "recovery_ms": round((time.monotonic() - t0) * 1000),
                  "error": str(exc)[:160]}
            self.events.append(ev)
            self._emit({"progress": "failover", **ev})
            return inp[-T:], len(new)
        raise RouteError(f"couldn't re-route layers {bad.a + 1}–{bad.b}")

    async def crosscheck(self) -> dict | None:
        """Re-run one hop's prefill on an independent replica and compare. A mismatch means one
        of the two lied (or is broken); a third replica breaks the tie, and the liar is dropped."""
        from ..node import RouteError

        spans = [s for s in planner.spans_for(self.node, self.m.id, self.exclude_self) if s.free > 0]
        options = []
        for idx, hop in enumerate(self.hops):
            if hop.prefill_out is None or hop.inputs is None:
                continue
            alts = [s for s in spans if s.node != hop.span.node and s.node not in self.excluded and s.start <= hop.a and s.end >= hop.b]
            if alts:
                options.append((idx, hop, alts))
        if not options:
            return None
        idx, hop, alts = random.choice(options)
        random.shuffle(alts)   # unpredictable checkers: a cheater can't know when it's being audited
        expected = hop.prefill_out
        verdicts = []
        for alt in alts[:2]:
            checker = _Hop(alt, hop.a, hop.b)
            try:
                got = await self._call(checker, 0, hop.inputs[:expected.shape[0]])
            except RouteError:
                continue
            finally:
                self.closed.append(checker)
            diff = float(np.max(np.abs(got - expected)) / (np.max(np.abs(expected)) + 1e-6))
            verdicts.append((alt.node, diff, got))
            if diff <= 1e-3:
                break
        if not verdicts:
            return None
        ok = verdicts[0][1] <= 1e-3
        ev = {"kind": "crosscheck", "layers": [hop.a, hop.b], "node": hop.span.node, "checker": verdicts[0][0],
              "ok": ok, "max_rel_diff": round(verdicts[0][1], 6)}
        if not ok and len(verdicts) > 1:
            # Two checkers: if they agree with each other, the chain's node is the odd one out.
            a, b = verdicts[0][2], verdicts[1][2]
            liar = hop.span.node if float(np.max(np.abs(a - b))) <= 1e-3 * (np.max(np.abs(a)) + 1e-6) else verdicts[0][0]
            ev.update(tiebreak=verdicts[1][0], liar=liar)
            rec = self.node.membership.peers.get(liar)
            if rec is not None:
                rec.failed += 10   # strong local down-rank: this node returned wrong math
            if liar == hop.span.node:
                # Replace the liar, then rebuild every later stage from the corrected activations
                # (their caches were computed from wrong inputs).
                self.excluded.add(liar)
                ev["replaced"] = True
                x, added = await self._failover(idx, _route_error("failed a cross-check"), 0, hop.inputs.shape[0])
                for later in self.hops[idx + added:]:
                    later.inputs = x
                    later.sid = secrets.token_hex(8)
                    x = await self._call(later, 0, x)
                    later.prefill_out = x
                self.refreshed = x
        self.events.append(ev)
        self._emit({"progress": "crosscheck", **ev})
        return ev

    async def close(self) -> None:
        async def one(h: _Hop):
            if h.span.local:
                self.node.swarm_host.sessions.pop((self.node.id, h.sid), None)
                return
            try:
                await self.node.rpc_one(h.span.node, h.span.hint, "stage.close", {"session": h.sid}, timeout=5)
            except Exception:
                pass
        await asyncio.gather(*(one(h) for h in self.hops + self.closed))

    def trace(self) -> dict:
        hops = []
        for h in self.hops:
            rec = self.node.membership.peers.get(h.span.node)
            body = self.node.advert()["body"] if h.span.local else (rec.body if rec else h.span.hint)
            v = self.node.hop_view(h.span.node, body, rec)
            v.update(layers=[h.a, h.b], calls=h.calls,
                     compute_ms=round(h.compute_ms / max(1, h.calls), 2), net_ms=round(h.net_ms / max(1, h.calls), 1),
                     transport="local" if h.span.local else self.node.transport_label(body).get("transport"))
            hops.append(v)
        return {"mode": "swarm", "layers": self.m.layers, "hops": hops, "events": self.events, "e2e": True,
                "text_stays_local": True, "express": {"steps": self.express_steps, "fallbacks": self.express_fallbacks}}


class SwarmClient:
    def __init__(self, node: "Node"):
        self.node = node
        self._real: dict[str, tuple] = {}
        self._waiting: dict[str, tuple[list[str], asyncio.Queue]] = {}

    def expect(self, tag: str, nodes: list[str]) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._waiting[tag] = (nodes, q)
        return q

    def forget(self, tag: str) -> None:
        self._waiting.pop(tag, None)

    async def rpc_result(self, sender: str, body: dict) -> AsyncIterator[dict]:
        """A stage's output on the express lane. Only the stage at that position may deliver it
        (errors about the next stage come from the one before it)."""
        entry = self._waiting.get(str(body.get("tag", "")))
        if entry is not None:
            nodes, q = entry
            idx = int(body.get("idx", -1))
            expected = nodes[idx - 1] if "error" in body and idx > 0 else (nodes[idx] if 0 <= idx < len(nodes) else None)
            if sender == expected:
                q.put_nowait(body)
        yield {"ok": True}

    def _kind(self) -> str:
        k = self.node.swarm_host.engine_kind()
        return "torch" if k == "torch" else "gguf"

    async def generate(self, model_id: str, messages: list[dict], max_tokens: int, temperature: float,
                       tools: list[dict] | None, exclude_self: bool = False) -> AsyncIterator[dict]:
        from ..node import RouteError

        m = catalog.BY_ID.get(model_id)
        if m is None or not m.swarm:
            raise RouteError(f"{model_id} can't run as a swarm")
        spans = planner.spans_for(self.node, model_id, exclude_self)
        chain = planner.plan(spans, m.layers)
        for _ in range(3):   # a host may be mid-move; give fresh adverts a moment before giving up
            if chain or not spans:
                break
            await asyncio.sleep(max(0.3, 1.5 * self.node.cfg.mesh.gossip_interval_s))
            spans = planner.spans_for(self.node, model_id, exclude_self)
            chain = planner.plan(spans, m.layers)
        if not chain:
            cov = planner.coverage(spans, m.layers)
            gaps = planner.missing_ranges(cov)
            pct = round(100 * (1 - sum(b - a for a, b in gaps) / m.layers))
            gap_txt = ", ".join(f"{a + 1}–{b}" for a, b in gaps[:3])
            raise RouteError(f"The {m.name} swarm is {pct}% assembled — layers {gap_txt} have no free host right now")
        progress: asyncio.Queue = asyncio.Queue()
        pipe = Pipeline(self.node, m, chain, exclude_self, progress)
        yield {"progress": "route", "hops": pipe.describe(), "layers": m.layers, "model": model_id,
               "est_ms_per_token": round(planner.chain_ms(chain), 1)}
        mock = self.node.swarm_host.engine_kind() == "mock"
        inner = self._run_mock(pipe, m, messages, max_tokens, temperature, tools) if mock else \
            self._run_real(pipe, m, messages, max_tokens, temperature, tools)
        try:
            async for ev in _merge(inner, progress):
                yield ev
        finally:
            await pipe.close()

    # ------------------------------------------------------------------ simulated swarms
    async def _run_mock(self, pipe: Pipeline, m, messages, max_tokens, temperature, tools) -> AsyncIterator[dict]:
        from ..backends import MockBackend

        t0 = time.monotonic()
        sid = m.id + "@swarm"
        events = [ev async for ev in MockBackend([sid], 1e6).generate(sid, messages, max_tokens, temperature, tools)]
        book = MockBookends(m.layers)
        prompt_len = max(8, sum(len(x.get("content") or "") for x in messages) // 4)
        await pipe.forward(book.embed_text([f"p{i}" for i in range(prompt_len)]), 0, prefill=True)
        check = await pipe.crosscheck() if self.node.cfg.swarm.crosscheck else None
        first, pos, i, rounds, proposed, accepted = None, prompt_len, 0, 0, 0, 0
        rng = random.Random(prompt_len)
        spec = self.node.cfg.swarm.draft
        while i < len(events) and i < max_tokens:
            k = SPEC_K if spec else 0
            n_acc = 0
            while n_acc < k and rng.random() < MOCK_ACCEPT:
                n_acc += 1
            emit = events[i:i + n_acc + 1]
            x = book.embed_text([json.dumps(e, sort_keys=True) for e in events[i:i + k + 1]] or ["eos"])
            await (pipe.forward_express(x, pos) if self.node.cfg.swarm.express else pipe.forward(x, pos))
            rounds += 1
            proposed += k
            accepted += min(n_acc, len(emit) - 1) if emit else 0
            pos += len(emit)
            i += len(emit)
            first = first or time.monotonic()
            for ev in emit:
                yield ev
        n = min(i, len(events))
        yield self._done(pipe, m, n, t0, first, "stop" if n < max_tokens else "length", check,
                         {"draft": m.draft or "simulated", "proposed": proposed, "accepted": accepted, "rounds": rounds} if spec else None)

    # ------------------------------------------------------------------ real weights
    def _real_models(self, m):
        """(bookends, tokenizer, draft) for a model whose GGUF is on this device."""
        if m.id in self._real:
            return self._real[m.id]
        from .engine import make_bookends
        from .gguf import GGUF
        from .tokenizer import Tokenizer

        host = self.node.swarm_host
        path = host.weights_path(m.id) or host.slice_path(m.id, 0, 0)
        if not path.exists():
            raise _route_error(f"NEED_BOOKENDS {m.bookend_gb}")
        g = GGUF(path)
        draft = None
        if self.node.cfg.swarm.draft and m.draft:
            dp = host.weights_path(m.draft)
            if dp is not None:
                draft = (GGUF(dp), )
        out = (make_bookends(self._kind(), g), Tokenizer.from_gguf(g), draft)
        self._real[m.id] = out
        return out

    async def _run_real(self, pipe: Pipeline, m, messages, max_tokens, temperature, tools) -> AsyncIterator[dict]:
        from .engine import LocalModelAny
        from .tokenizer import OutputParser, StreamDecoder, render_chat

        t0 = time.monotonic()
        host = self.node.swarm_host
        if not (host.weights_path(m.id) or host.slice_path(m.id, 0, 0)).exists():
            # First use: fetch just the embedding + head (+ tokenizer) — the parts that keep text on this device.
            pipe._emit({"progress": "bookends", "gb": m.bookend_gb})
            await host._download(m.id, 0, 0, host.slice_path(m.id, 0, 0), bookends=True, track=False)
        bookends, tok, draft_src = await asyncio.to_thread(self._real_models, m)
        ids = tok.encode(render_chat(m.arch, messages, tools))
        ctx = min(bookends.cfg.ctx, 8192)
        if len(ids) >= ctx:
            raise _route_error("conversation is too long for this model's context")
        max_tokens = min(max_tokens, ctx - len(ids))
        x = await pipe.forward(bookends.embed_ids(ids), 0, prefill=True)
        check = await pipe.crosscheck() if self.node.cfg.swarm.crosscheck else None
        if pipe.refreshed is not None:
            x = pipe.refreshed
        logits = await asyncio.to_thread(bookends.logits, x[-1:])
        stops = tok.stop_ids()
        rng = np.random.default_rng()
        dec, parser = StreamDecoder(tok), OutputParser()
        draft = LocalModelAny(draft_src[0], self._kind()) if draft_src else None
        pos, produced, first, finish = len(ids), 0, None, "length"
        spec = {"draft": m.draft, "proposed": 0, "accepted": 0, "rounds": 0} if draft else None
        nxt = _sample(logits[0], temperature, rng)
        if draft is not None:
            await asyncio.to_thread(draft.step, ids, 0)
        draft_pending: list[int] = []
        while produced < max_tokens:
            if nxt in stops:
                finish = "stop"
                break
            # Tokens to emit this round: `nxt`, plus any speculative drafts the swarm accepts.
            emit = [nxt]
            if draft is None:
                out = await self._step(pipe, bookends.embed_ids([nxt]), pos)
                logits = await asyncio.to_thread(bookends.logits, out)
                nxt = _sample(logits[-1], temperature, rng)
                pos += 1
            else:
                k = min(SPEC_K, max_tokens - produced - 1)
                props, qs = await asyncio.to_thread(_propose, draft, draft_pending + [nxt], k, temperature, rng)
                out = await self._step(pipe, bookends.embed_ids([nxt] + props), pos)
                lg = await asyncio.to_thread(bookends.logits, out)
                n_acc, nxt = _verify(props, qs, lg, temperature, rng)
                emit += props[:n_acc]
                spec["proposed"] += len(props)
                spec["accepted"] += n_acc
                spec["rounds"] += 1
                pos += 1 + n_acc
                # Roll the draft back to the accepted prefix.
                draft.cache.length = min(draft.cache.length, pos)
                draft_pending = []
                if n_acc == len(props) and props:
                    draft_pending = [props[-1]]
                    draft.cache.length = min(draft.cache.length, pos - 1)
            first = first or time.monotonic()
            stop_now = False
            for t in emit:
                if t in stops:
                    stop_now, finish = True, "stop"
                    break
                produced += 1
                for ev in parser.feed(dec.push(t)):
                    yield ev
                if produced >= max_tokens:
                    break
            if stop_now:
                break
        for ev in parser.flush():
            yield ev
        if parser.calls:
            finish = "tool_calls"
            yield {"tool_calls": parser.calls}
        yield self._done(pipe, m, produced, t0, first, finish, check, spec)

    async def _step(self, pipe: Pipeline, x: np.ndarray, pos: int) -> np.ndarray:
        return await (pipe.forward_express(x, pos) if self.node.cfg.swarm.express else pipe.forward(x, pos))

    def _done(self, pipe: Pipeline, m, n: int, t0: float, first: float | None, finish: str, check, spec) -> dict:
        self.node.counters["swarm_replies"] += 1
        elapsed = time.monotonic() - (first or time.monotonic())
        tps = (n - 1) / elapsed if first and n > 1 and elapsed > 0 else None
        path = pipe.trace()
        if spec:
            path["speculative"] = spec
        if check:
            path["crosscheck"] = check
        nodes = {h["node"] for h in path["hops"]}
        return {"done": True, "finish_reason": finish, "tokens": n, "model": m.id, "mode": "swarm",
                "served_by": "swarm", "local": False, "receipt_verified": True,
                "tokens_per_s": round(tps, 1) if tps else None,
                "latency_ms": round(((first or time.monotonic()) - t0) * 1000),
                "swarm_nodes": len(nodes), "path": path}


def _softmax(z: np.ndarray, temperature: float) -> np.ndarray:
    z = z.astype(np.float64) / max(temperature, 1e-6)
    z = np.exp(z - z.max())
    return z / z.sum()


def _sample(logits: np.ndarray, temperature: float, rng, top_p: float = 0.95) -> int:
    if temperature <= 0:
        return int(np.argmax(logits))
    p = _softmax(logits, temperature)
    order = np.argsort(-p)
    keep = order[: max(1, int(np.searchsorted(np.cumsum(p[order]), top_p)) + 1)]
    q = p[keep] / p[keep].sum()
    return int(rng.choice(keep, p=q))


def _propose(draft, feed: list[int], k: int, temperature: float, rng) -> tuple[list[int], list]:
    """Draft model proposes k tokens after `feed` (tokens not yet in its cache)."""
    props, qs = [], []
    pos = draft.cache.length
    logits = draft.step(feed, pos)[-1]
    pos += len(feed)
    for j in range(k):
        q = _softmax(logits, temperature) if temperature > 0 else None
        t = int(np.argmax(logits)) if temperature <= 0 else int(rng.choice(len(q), p=q))
        props.append(t)
        qs.append(q)
        if j < k - 1:
            logits = draft.step([t], pos)[-1]
            pos += 1
    return props, qs


def _verify(props: list[int], qs: list, target_logits: np.ndarray, temperature: float, rng) -> tuple[int, int]:
    """Standard speculative-decoding acceptance. Returns (accepted count, next token).
    Greedy: identical output to plain decoding. Sampled: identical distribution."""
    for i, d in enumerate(props):
        if temperature <= 0:
            if int(np.argmax(target_logits[i])) != d:
                return i, int(np.argmax(target_logits[i]))
            continue
        p = _softmax(target_logits[i], temperature)
        if rng.random() < min(1.0, p[d] / max(qs[i][d], 1e-12)):
            continue
        resid = np.maximum(p - qs[i], 0)
        resid = resid / resid.sum() if resid.sum() > 0 else p
        return i, int(rng.choice(len(resid), p=resid))
    return len(props), _sample(target_logits[len(props)], temperature, rng)


async def _merge(inner: AsyncIterator[dict], progress: asyncio.Queue) -> AsyncIterator[dict]:
    """Interleave progress events (plan, prefill per stage, failovers) with the reply stream."""
    it = inner.__aiter__()
    nxt = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            getter = asyncio.ensure_future(progress.get())
            done, _ = await asyncio.wait({nxt, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
            else:
                getter.cancel()
            if nxt in done:
                try:
                    ev = nxt.result()
                except StopAsyncIteration:
                    break
                while not progress.empty():
                    yield progress.get_nowait()
                yield ev
                nxt = asyncio.ensure_future(it.__anext__())
    finally:
        if not nxt.done():
            nxt.cancel()
        await it.aclose() if hasattr(it, "aclose") else None

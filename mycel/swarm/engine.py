"""The math of a swarm stage: transformer blocks for a contiguous layer range, plus the
requester-side "bookends" (embedding lookup, final norm, LM head).

``GGUFStage`` runs real weights (Llama 3.x and Qwen3 dense architectures) in NumPy float32.
Splitting the layer loop across machines is exact: given the same inputs, a chain of stages
produces bit-identical activations to running every layer in one process, because each stage
receives the previous stage's float32 output unchanged.

``MockStage`` / ``MockBookends`` stand in for real weights in large simulated meshes: they move
real activation tensors through the real protocol and burn simulated compute time computed
from each node's memory bandwidth, so latency, failover and verification behave realistically.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import numpy as np

from .gguf import GGUF


@dataclass
class ArchConfig:
    arch: str
    n_layers: int
    d_model: int
    n_heads: int
    n_kv: int
    head_dim: int
    eps: float
    rope_base: float
    rope_neox: bool
    qk_norm: bool
    vocab: int
    ctx: int

    @classmethod
    def from_gguf(cls, g: GGUF) -> "ArchConfig":
        arch = g.arch
        if arch not in ("llama", "qwen3", "qwen2"):
            raise ValueError(f"architecture {arch!r} isn't supported for swarm inference yet")
        d = int(g.meta("embedding_length"))
        heads = int(g.meta("attention.head_count"))
        return cls(arch=arch, n_layers=int(g.meta("block_count")), d_model=d, n_heads=heads,
                   n_kv=int(g.meta("attention.head_count_kv", heads)),
                   head_dim=int(g.meta("attention.key_length", d // heads)),
                   eps=float(g.meta("attention.layer_norm_rms_epsilon", 1e-5)),
                   rope_base=float(g.meta("rope.freq_base", 10000.0)),
                   rope_neox=arch in ("qwen3", "qwen2"), qk_norm=g.has("blk.0.attn_q_norm.weight"),
                   vocab=len(g.kv.get("tokenizer.ggml.tokens", [])) or int(g.meta("vocab_size", 0)),
                   ctx=int(g.meta("context_length", 8192)))


def rmsnorm(x: np.ndarray, w: np.ndarray, eps: float) -> np.ndarray:
    return x * (1.0 / np.sqrt(np.mean(x * x, axis=-1, keepdims=True) + eps)) * w


class KVCache:
    """Per-session keys/values for one stage's layers. Truncating to ``pos`` is free, which is what
    makes speculative decoding (roll back rejected drafts) and replay-on-failover cheap."""

    def __init__(self, n_layers: int, n_kv: int, head_dim: int, cap: int = 256):
        self.k = np.zeros((n_layers, n_kv, cap, head_dim), np.float32)
        self.v = np.zeros_like(self.k)
        self.length = 0

    def ensure(self, n: int) -> None:
        cap = self.k.shape[2]
        if n <= cap:
            return
        new = max(n, cap * 2)
        pad = ((0, 0), (0, 0), (0, new - cap), (0, 0))
        self.k, self.v = np.pad(self.k, pad), np.pad(self.v, pad)

    @property
    def nbytes(self) -> int:
        return self.k.nbytes + self.v.nbytes


class GGUFStage:
    """Transformer blocks [start, end) of a GGUF model, dequantized to float32."""

    def __init__(self, g: GGUF, start: int, end: int):
        self.cfg = c = ArchConfig.from_gguf(g)
        if not 0 <= start < end <= c.n_layers:
            raise ValueError(f"layer range {start}-{end} outside 0-{c.n_layers}")
        self.start, self.end = start, end
        self.layers = [self._load(g, i) for i in range(start, end)]
        inv = c.rope_base ** (-np.arange(0, c.head_dim, 2, dtype=np.float64) / c.head_dim)
        if g.has("rope_freqs.weight"):  # Llama 3.1+ long-context frequency factors
            inv = inv / g.array("rope_freqs.weight").astype(np.float64)
        self.inv_freq = inv
        self.nbytes = sum(sum(a.nbytes for a in L.values() if isinstance(a, np.ndarray)) for L in self.layers)

    @staticmethod
    def _load(g: GGUF, i: int) -> dict:
        p = f"blk.{i}."
        names = ["attn_norm", "attn_q", "attn_k", "attn_v", "attn_output", "ffn_norm", "ffn_gate", "ffn_up", "ffn_down",
                 "attn_q_norm", "attn_k_norm"]
        L = {n: g.array(p + n + ".weight") for n in names if g.has(p + n + ".weight")}
        for n in ("attn_q", "attn_k", "attn_v"):
            if g.has(p + n + ".bias"):
                L[n + "_b"] = g.array(p + n + ".bias")
        return L

    def new_cache(self) -> KVCache:
        return KVCache(self.end - self.start, self.cfg.n_kv, self.cfg.head_dim)

    def _rope(self, x: np.ndarray, cos: np.ndarray, sin: np.ndarray) -> np.ndarray:
        if self.cfg.rope_neox:
            h = x.shape[-1] // 2
            x1, x2 = x[..., :h], x[..., h:]
            return np.concatenate([x1 * cos - x2 * sin, x2 * cos + x1 * sin], axis=-1)
        xe, xo = x[..., 0::2], x[..., 1::2]
        out = np.empty_like(x)
        out[..., 0::2] = xe * cos - xo * sin
        out[..., 1::2] = xe * sin + xo * cos
        return out

    def forward(self, x: np.ndarray, pos: int, cache: KVCache, start: int | None = None, end: int | None = None) -> np.ndarray:
        """Run layers [start, end) (default: all hosted) for T new positions starting at ``pos``.
        The cache is first truncated to ``pos`` — anything after it is discarded."""
        start = self.start if start is None else start
        end = self.end if end is None else end
        if not self.start <= start < end <= self.end:
            raise ValueError("requested layers are not hosted here")
        if pos > cache.length:
            raise ValueError("gap in context: replay from position 0")
        c = self.cfg
        x = np.ascontiguousarray(x, dtype=np.float32)
        T = x.shape[0]
        cache.ensure(pos + T)
        ang = np.arange(pos, pos + T, dtype=np.float64)[:, None] * self.inv_freq[None, :]
        cos = np.cos(ang).astype(np.float32)[:, None, :]
        sin = np.sin(ang).astype(np.float32)[:, None, :]
        S, group, scale = pos + T, c.n_heads // c.n_kv, float(1.0 / np.sqrt(c.head_dim))  # python float: keeps float32
        mask = np.arange(S)[None, :] > (pos + np.arange(T))[:, None]
        for li in range(start - self.start, end - self.start):
            L = self.layers[li]
            h = rmsnorm(x, L["attn_norm"], c.eps)
            q = h @ L["attn_q"].T
            k = h @ L["attn_k"].T
            v = h @ L["attn_v"].T
            if "attn_q_b" in L:
                q, k, v = q + L["attn_q_b"], k + L["attn_k_b"], v + L["attn_v_b"]
            q = q.reshape(T, c.n_heads, c.head_dim)
            k = k.reshape(T, c.n_kv, c.head_dim)
            v = v.reshape(T, c.n_kv, c.head_dim)
            if "attn_q_norm" in L:
                q = rmsnorm(q, L["attn_q_norm"], c.eps)
                k = rmsnorm(k, L["attn_k_norm"], c.eps)
            q, k = self._rope(q, cos, sin), self._rope(k, cos, sin)
            cache.k[li, :, pos:pos + T] = k.transpose(1, 0, 2)
            cache.v[li, :, pos:pos + T] = v.transpose(1, 0, 2)
            K, V = cache.k[li, :, :S], cache.v[li, :, :S]                       # (kv, S, hd)
            qg = q.transpose(1, 0, 2).reshape(c.n_kv, group, T, c.head_dim)     # (kv, g, T, hd)
            att = (qg @ K[:, None].transpose(0, 1, 3, 2)) * scale                # (kv, g, T, S)
            att = np.where(mask, -np.inf, att)
            att = np.exp(att - att.max(axis=-1, keepdims=True))
            att /= att.sum(axis=-1, keepdims=True)
            o = (att @ V[:, None]).reshape(c.n_heads, T, c.head_dim).transpose(1, 0, 2).reshape(T, -1)
            x = x + o @ L["attn_output"].T
            h = rmsnorm(x, L["ffn_norm"], c.eps)
            g_ = h @ L["ffn_gate"].T
            x = x + ((g_ / (1.0 + np.exp(-g_))) * (h @ L["ffn_up"].T)) @ L["ffn_down"].T
        cache.length = pos + T
        return x


class GGUFBookends:
    """Embedding, final norm and LM head — run by the requester, so text never leaves it."""

    def __init__(self, g: GGUF):
        self.cfg = ArchConfig.from_gguf(g)
        self.embed = g.array("token_embd.weight")
        self.norm = g.array("output_norm.weight")
        self.head = g.array("output.weight") if g.has("output.weight") else self.embed   # tied embeddings
        self.nbytes = self.embed.nbytes + (self.head.nbytes if self.head is not self.embed else 0)

    def embed_ids(self, ids: list[int]) -> np.ndarray:
        return self.embed[np.asarray(ids, dtype=np.int64)]

    def logits(self, h: np.ndarray) -> np.ndarray:
        """h: (T, D) -> (T, vocab) float32."""
        return rmsnorm(h, self.norm, self.cfg.eps) @ self.head.T


class LocalModel:
    """A whole model in one process (draft models for speculative decoding, and reference runs)."""

    def __init__(self, g: GGUF):
        self.bookends = GGUFBookends(g)
        self.stage = GGUFStage(g, 0, self.bookends.cfg.n_layers)
        self.cache = self.stage.new_cache()

    def step(self, ids: list[int], pos: int) -> np.ndarray:
        return self.bookends.logits(self.stage.forward(self.bookends.embed_ids(ids), pos, self.cache))


# ---------------------------------------------------------------------------- simulation engines
MOCK_DIM = 16


def mock_layer_delta(layer: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(f"mock-layer-{layer}".encode()).digest()[:4], "big")
    return np.random.default_rng(seed).standard_normal(MOCK_DIM).astype(np.float32) * 0.01


class MockStage:
    """A stage with simulated compute. Each layer applies a fixed deterministic update, so outputs
    are verifiable by any replica (and a lying node is caught by cross-checks)."""

    def __init__(self, n_layers: int, start: int, end: int, ms_per_layer: float, byzantine: bool = False):
        self.start, self.end, self.ms_per_layer, self.byzantine = start, end, ms_per_layer, byzantine
        self.deltas = {i: mock_layer_delta(i) for i in range(start, end)}
        self.nbytes = 0

    def new_cache(self):
        return KVCache(1, 1, 1, cap=1)

    def compute_seconds(self, T: int, n_layers: int) -> float:
        # Decode is memory-bound (one pass over the weights); prefill batches T tokens per pass.
        return self.ms_per_layer * n_layers * (1 + 0.04 * (T - 1)) / 1000

    def forward(self, x: np.ndarray, pos: int, cache, start: int | None = None, end: int | None = None) -> np.ndarray:
        start = self.start if start is None else start
        end = self.end if end is None else end
        if not self.start <= start < end <= self.end:
            raise ValueError("requested layers are not hosted here")
        if pos > cache.length:
            raise ValueError("gap in context: replay from position 0")
        x = np.asarray(x, np.float32).copy()
        for i in range(start, end):
            x = np.tanh(x + self.deltas[i])
        if self.byzantine:
            x = x + 0.5
        cache.length = pos + x.shape[0]
        return x


class MockBookends:
    """Requester side for simulated swarms: deterministic activations derived from the text."""

    def __init__(self, n_layers: int):
        self.n_layers = n_layers

    def embed_text(self, pieces: list[str]) -> np.ndarray:
        out = np.empty((len(pieces), MOCK_DIM), np.float32)
        for i, p in enumerate(pieces):
            seed = int.from_bytes(hashlib.sha256(p.encode()).digest()[:4], "big")
            out[i] = np.random.default_rng(seed).standard_normal(MOCK_DIM) * 0.1
        return out


def time_now_ms() -> float:
    return time.perf_counter() * 1000


# ---------------------------------------------------------------------------- GPU engine (PyTorch)
def torch_device() -> str | None:
    """Best accelerator PyTorch can use here, or None (then the NumPy engine runs on the CPU)."""
    try:
        import torch
    except ImportError:
        return None
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return None


class TorchKV:
    def __init__(self, torch, n_layers, n_kv, head_dim, device, dtype, cap=256):
        self.torch, self.device, self.dtype = torch, device, dtype
        self.k = torch.zeros((n_layers, n_kv, cap, head_dim), device=device, dtype=dtype)
        self.v = torch.zeros_like(self.k)
        self.length = 0

    def ensure(self, n: int) -> None:
        cap = self.k.shape[2]
        if n <= cap:
            return
        new = max(n, cap * 2)
        pad = self.torch.zeros((self.k.shape[0], self.k.shape[1], new - cap, self.k.shape[3]), device=self.device, dtype=self.dtype)
        self.k, self.v = self.torch.cat([self.k, pad], 2), self.torch.cat([self.v, pad.clone()], 2)


class TorchStage:
    """The same transformer blocks as ``GGUFStage``, on the GPU (Metal or CUDA) in fp16.

    Weights are held in fp16 (half the memory of the NumPy engine), Q/K/V and gate/up are fused
    into single matmuls, RoPE tables live on the device and attention uses the fused kernel —
    at batch size 1 the cost is kernel launches, so fewer, bigger ops win. Splitting is still
    exact *within* this engine; against the fp32 engine, outputs agree to fp16 precision."""

    MAX_POS = 32768

    def __init__(self, g: GGUF, start: int, end: int, device: str | None = None):
        import torch
        self.torch = torch
        self.F = torch.nn.functional
        self.cfg = c = ArchConfig.from_gguf(g)
        if not 0 <= start < end <= c.n_layers:
            raise ValueError(f"layer range {start}-{end} outside 0-{c.n_layers}")
        self.start, self.end = start, end
        self.device = device or torch_device() or "cpu"
        self.dtype = torch.float16 if self.device != "cpu" else torch.float32
        self.layers = [self._load(g, i) for i in range(start, end)]
        inv = c.rope_base ** (-np.arange(0, c.head_dim, 2, dtype=np.float64) / c.head_dim)
        if g.has("rope_freqs.weight"):
            inv = inv / g.array("rope_freqs.weight").astype(np.float64)
        self.inv_freq = inv
        self._rope_len = 0
        self._ensure_rope(4096)
        self.nbytes = sum(t.numel() * t.element_size() for L in self.layers for t in L.values() if hasattr(t, "numel"))

    def _ensure_rope(self, n: int) -> None:
        if n <= self._rope_len:
            return
        n = min(self.MAX_POS, max(n, 2 * self._rope_len))
        ang = np.arange(n, dtype=np.float64)[:, None] * self.inv_freq[None, :]
        self.cos = self.torch.from_numpy(np.cos(ang).astype(np.float32)).to(self.device)
        self.sin = self.torch.from_numpy(np.sin(ang).astype(np.float32)).to(self.device)
        self._rope_len = n

    def _load(self, g: GGUF, i: int) -> dict:
        t, p = self.torch, f"blk.{i}."
        w = lambda n: g.array(p + n + ".weight")
        out = {}
        for n in ("attn_norm", "ffn_norm", "attn_q_norm", "attn_k_norm"):
            if g.has(p + n + ".weight"):
                out[n] = t.from_numpy(w(n)).to(self.device, t.float32)
        out["qkv"] = t.from_numpy(np.concatenate([w("attn_q"), w("attn_k"), w("attn_v")], 0)).to(self.device, self.dtype)
        if g.has(p + "attn_q.bias"):
            out["qkv_b"] = t.from_numpy(np.concatenate([g.array(p + f"attn_{x}.bias") for x in "qkv"])).to(self.device, self.dtype)
        out["o"] = t.from_numpy(w("attn_output")).to(self.device, self.dtype)
        out["gu"] = t.from_numpy(np.concatenate([w("ffn_gate"), w("ffn_up")], 0)).to(self.device, self.dtype)
        out["down"] = t.from_numpy(w("ffn_down")).to(self.device, self.dtype)
        return out

    def new_cache(self) -> TorchKV:
        return TorchKV(self.torch, self.end - self.start, self.cfg.n_kv, self.cfg.head_dim, self.device, self.dtype)

    def _rope(self, x, cos, sin):
        if self.cfg.rope_neox:
            h = x.shape[-1] // 2
            x1, x2 = x[..., :h], x[..., h:]
            return self.torch.cat([x1 * cos - x2 * sin, x2 * cos + x1 * sin], -1)
        xe, xo = x[..., 0::2], x[..., 1::2]
        return self.torch.stack([xe * cos - xo * sin, xe * sin + xo * cos], -1).flatten(-2)

    def forward(self, x: np.ndarray, pos: int, cache: TorchKV, start: int | None = None, end: int | None = None) -> np.ndarray:
        t, c, F = self.torch, self.cfg, self.F
        start = self.start if start is None else start
        end = self.end if end is None else end
        if not self.start <= start < end <= self.end:
            raise ValueError("requested layers are not hosted here")
        if pos > cache.length:
            raise ValueError("gap in context: replay from position 0")
        nq, nk = c.n_heads * c.head_dim, c.n_kv * c.head_dim
        with t.no_grad():
            h_ = t.from_numpy(np.array(x, dtype=np.float32)).to(self.device)
            T = h_.shape[0]
            cache.ensure(pos + T)
            self._ensure_rope(pos + T)
            cos, sin = self.cos[pos:pos + T][:, None, :], self.sin[pos:pos + T][:, None, :]
            S = pos + T
            mask = None
            if T > 1:   # causal over the new tokens; everything cached is visible
                mask = t.ones((T, S), dtype=t.bool, device=self.device).tril(diagonal=pos)
            for li in range(start - self.start, end - self.start):
                L = self.layers[li]
                a = F.rms_norm(h_, (c.d_model,), L["attn_norm"], c.eps).to(self.dtype)
                qkv = a @ L["qkv"].T
                if "qkv_b" in L:
                    qkv = qkv + L["qkv_b"]
                q = qkv[:, :nq].float().view(T, c.n_heads, c.head_dim)
                k = qkv[:, nq:nq + nk].float().view(T, c.n_kv, c.head_dim)
                v = qkv[:, nq + nk:].view(T, c.n_kv, c.head_dim)
                if "attn_q_norm" in L:
                    q = F.rms_norm(q, (c.head_dim,), L["attn_q_norm"], c.eps)
                    k = F.rms_norm(k, (c.head_dim,), L["attn_k_norm"], c.eps)
                q, k = self._rope(q, cos, sin), self._rope(k, cos, sin)
                cache.k[li, :, pos:S] = k.transpose(0, 1).to(self.dtype)
                cache.v[li, :, pos:S] = v.transpose(0, 1)
                o = F.scaled_dot_product_attention(q.transpose(0, 1).to(self.dtype)[None], cache.k[li, :, :S][None],
                                                   cache.v[li, :, :S][None], attn_mask=mask, enable_gqa=True)
                o = o[0].transpose(0, 1).reshape(T, nq)
                h_ = h_ + (o @ L["o"].T).float()
                a = F.rms_norm(h_, (c.d_model,), L["ffn_norm"], c.eps).to(self.dtype)
                gu = a @ L["gu"].T
                g_, u = gu.chunk(2, -1)
                h_ = h_ + ((F.silu(g_) * u) @ L["down"].T).float()
            cache.length = S
            return h_.cpu().numpy()


class TorchBookends:
    """Embedding lookup and LM head on the GPU (fp16)."""

    def __init__(self, g: GGUF, device: str | None = None):
        import torch
        self.torch = torch
        self.cfg = ArchConfig.from_gguf(g)
        self.device = device or torch_device() or "cpu"
        dt = torch.float16 if self.device != "cpu" else torch.float32
        self.embed = torch.from_numpy(g.array("token_embd.weight")).to(self.device, dt)
        self.norm = torch.from_numpy(g.array("output_norm.weight")).to(self.device, torch.float32)
        self.head = torch.from_numpy(g.array("output.weight")).to(self.device, dt) if g.has("output.weight") else self.embed
        self.nbytes = self.embed.numel() * self.embed.element_size() * (1 if self.head is self.embed else 2)

    def embed_ids(self, ids: list[int]) -> np.ndarray:
        with self.torch.no_grad():
            return self.embed[self.torch.tensor(ids, device=self.device)].float().cpu().numpy()

    def logits(self, h: np.ndarray) -> np.ndarray:
        t = self.torch
        with t.no_grad():
            x = t.from_numpy(np.ascontiguousarray(h, np.float32)).to(self.device)
            x = x * t.rsqrt((x * x).mean(-1, keepdim=True) + self.cfg.eps) * self.norm
            return (x.to(self.head.dtype) @ self.head.T).float().cpu().numpy()


def make_stage(kind: str, g: GGUF, start: int, end: int):
    return TorchStage(g, start, end) if kind == "torch" else GGUFStage(g, start, end)


def make_bookends(kind: str, g: GGUF):
    return TorchBookends(g) if kind == "torch" else GGUFBookends(g)


class LocalModelAny:
    """A whole model in one process on either engine (draft models, references)."""

    def __init__(self, g: GGUF, kind: str = "gguf"):
        self.bookends = make_bookends(kind, g)
        self.stage = make_stage(kind, g, 0, self.bookends.cfg.n_layers)
        self.cache = self.stage.new_cache()

    def step(self, ids: list[int], pos: int) -> np.ndarray:
        return self.bookends.logits(self.stage.forward(self.bookends.embed_ids(ids), pos, self.cache))

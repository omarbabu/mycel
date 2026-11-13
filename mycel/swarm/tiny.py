"""Write tiny, random, *real-format* GGUF models (byte-level BPE vocab, Q8_0 weights).

Tests and demos use them to exercise the exact code path real models take — GGUF parsing,
dequantization, tokenization, chat templates, stage math, failover and speculation — in
milliseconds. They produce gibberish, deterministically.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .gguf import F32, Q8_0, quantize_q8_0, write_gguf
from .tokenizer import B2U

SPECIAL = ["<|endoftext|>", "<|im_start|>", "<|im_end|>", "<think>", "</think>", "<tool_call>", "</tool_call>"]
MERGES = [("Ġ", "t"), ("h", "e"), ("Ġt", "he"), ("i", "n"), ("e", "r"), ("a", "n"), ("o", "n"), ("Ġ", "a"),
          ("r", "e"), ("Ġ", "s"), ("e", "n"), ("a", "t"), ("o", "r"), ("Ġ", "w"), ("i", "s")]


def make_tiny_gguf(path: str | Path, arch: str = "qwen3", layers: int = 8, d: int = 64, heads: int = 4,
                   kv: int = 2, ffn: int = 128, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    base = [B2U[b] for b in range(256)]
    tokens = base + ["".join(m) for m in MERGES] + SPECIAL
    types = [1] * (len(tokens) - len(SPECIAL)) + [3] * len(SPECIAL)
    V, hd = len(tokens), d // heads
    kvs = {
        "general.architecture": arch, "general.name": f"tiny-{arch}-{seed}",
        f"{arch}.block_count": layers, f"{arch}.context_length": 2048, f"{arch}.embedding_length": d,
        f"{arch}.feed_forward_length": ffn, f"{arch}.attention.head_count": heads,
        f"{arch}.attention.head_count_kv": kv, f"{arch}.attention.key_length": hd,
        f"{arch}.rope.freq_base": 10000.0, f"{arch}.attention.layer_norm_rms_epsilon": 1e-6,
        "tokenizer.ggml.model": "gpt2", "tokenizer.ggml.pre": "qwen2" if arch == "qwen3" else "llama-bpe",
        "tokenizer.ggml.tokens": tokens, "tokenizer.ggml.token_type": types,
        "tokenizer.ggml.merges": [" ".join(m) for m in MERGES],
        "tokenizer.ggml.eos_token_id": tokens.index("<|im_end|>"), "tokenizer.ggml.bos_token_id": 0,
        "tokenizer.ggml.add_bos_token": False,
    }

    def mat(rows, cols, scale=None):
        w = rng.standard_normal((rows, cols)).astype(np.float32) * (scale or 1.0 / np.sqrt(cols))
        return ((cols, rows), Q8_0, quantize_q8_0(w))

    def vec(n, around=1.0):
        return ((n,), F32, (around + 0.05 * rng.standard_normal(n)).astype(np.float32).tobytes())

    emb = rng.standard_normal((V, d)).astype(np.float32) * 0.5
    emb[-len(SPECIAL):] *= 0.01   # special tokens rarely win greedy decoding
    tensors = [("token_embd.weight", (d, V), Q8_0, quantize_q8_0(emb)), ("output_norm.weight", *vec(d))]
    if arch == "llama":
        tensors.append(("output.weight", *mat(V, d)))
    for i in range(layers):
        p = f"blk.{i}."
        tensors += [(p + "attn_norm.weight", *vec(d)), (p + "attn_q.weight", *mat(heads * hd, d)),
                    (p + "attn_k.weight", *mat(kv * hd, d)), (p + "attn_v.weight", *mat(kv * hd, d)),
                    (p + "attn_output.weight", *mat(d, heads * hd)), (p + "ffn_norm.weight", *vec(d)),
                    (p + "ffn_gate.weight", *mat(ffn, d)), (p + "ffn_up.weight", *mat(ffn, d)),
                    (p + "ffn_down.weight", *mat(d, ffn))]
        if arch == "qwen3":
            tensors += [(p + "attn_q_norm.weight", *vec(hd)), (p + "attn_k_norm.weight", *vec(hd))]
    path = Path(path)
    write_gguf(path, kvs, tensors)
    return path

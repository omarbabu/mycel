"""Byte-level BPE tokenizer and chat templates, read straight from a model's GGUF metadata.

In swarm mode the *requester* tokenizes, embeds and samples on its own device, so no stage
ever sees text. This module is what makes that possible without extra dependencies.
"""
from __future__ import annotations

import codecs
import json
from functools import lru_cache

import regex

# Pre-tokenizer split patterns (as in llama.cpp's llama-vocab.cpp).
PRE = {
    "qwen2": r"(?:'[sS]|'[tT]|'[rR][eE]|'[vV][eE]|'[mM]|'[lL][lL]|'[dD])|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+",
    "llama-bpe": r"(?:'[sS]|'[tT]|'[rR][eE]|'[vV][eE]|'[mM]|'[lL][lL]|'[dD])|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+",
}
CONTROL, USER_DEFINED = 3, 4


def _bytes_to_unicode() -> dict[int, str]:
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs)}


B2U = _bytes_to_unicode()
U2B = {u: b for b, u in B2U.items()}


class Tokenizer:
    def __init__(self, tokens: list[str], token_types: list[int], merges: list[str], pre: str,
                 bos: int | None, eos: int | None, add_bos: bool):
        self.tokens = tokens
        self.vocab = {t: i for i, t in enumerate(tokens)}
        self.ranks = {tuple(m.split(" ", 1)): i for i, m in enumerate(merges)}
        self.split = regex.compile(PRE.get(pre, PRE["llama-bpe"]))
        self.special = {t: i for i, (t, ty) in enumerate(zip(tokens, token_types)) if ty in (CONTROL, USER_DEFINED)}
        self.special_ids = set(self.special.values())
        names = sorted(self.special, key=len, reverse=True)
        self.special_re = regex.compile("(" + "|".join(regex.escape(s) for s in names) + ")") if names else None
        self.bos, self.eos, self.add_bos = bos, eos, add_bos
        self._bpe = lru_cache(maxsize=65536)(self._bpe_uncached)

    @classmethod
    def from_gguf(cls, g) -> "Tokenizer":
        kv = g.kv
        if kv.get("tokenizer.ggml.model") != "gpt2":
            raise ValueError(f"unsupported tokenizer {kv.get('tokenizer.ggml.model')!r} (byte-level BPE only)")
        return cls(kv["tokenizer.ggml.tokens"], kv.get("tokenizer.ggml.token_type", [1] * len(kv["tokenizer.ggml.tokens"])),
                   kv.get("tokenizer.ggml.merges", []), kv.get("tokenizer.ggml.pre", "llama-bpe"),
                   kv.get("tokenizer.ggml.bos_token_id"), kv.get("tokenizer.ggml.eos_token_id"),
                   bool(kv.get("tokenizer.ggml.add_bos_token", False)))

    def _bpe_uncached(self, word: str) -> tuple[int, ...]:
        parts = list(word)
        while len(parts) > 1:
            best, idx = None, -1
            for i in range(len(parts) - 1):
                r = self.ranks.get((parts[i], parts[i + 1]))
                if r is not None and (best is None or r < best):
                    best, idx = r, i
            if best is None:
                break
            a, b = parts[idx], parts[idx + 1]
            merged, i = [], 0
            while i < len(parts):
                if i < len(parts) - 1 and parts[i] == a and parts[i + 1] == b:
                    merged.append(a + b)
                    i += 2
                else:
                    merged.append(parts[i])
                    i += 1
            parts = merged
        out = []
        for p in parts:
            if p in self.vocab:
                out.append(self.vocab[p])
            else:  # unreachable with a complete byte vocabulary; fall back to single bytes
                out.extend(self.vocab[c] for c in p if c in self.vocab)
        return tuple(out)

    def encode(self, text: str, special: bool = True) -> list[int]:
        ids: list[int] = []
        chunks = self.special_re.split(text) if (special and self.special_re) else [text]
        for chunk in chunks:
            if not chunk:
                continue
            if special and chunk in self.special:
                ids.append(self.special[chunk])
                continue
            for piece in self.split.findall(chunk):
                ids.extend(self._bpe("".join(B2U[b] for b in piece.encode("utf-8"))))
        return ids

    def token_bytes(self, tid: int) -> bytes:
        t = self.tokens[tid]
        if tid in self.special_ids:
            return t.encode()
        return bytes(U2B[c] for c in t if c in U2B)

    def decode(self, ids: list[int]) -> str:
        return b"".join(self.token_bytes(i) for i in ids).decode("utf-8", errors="replace")

    def stop_ids(self) -> set[int]:
        stops = {self.eos} if self.eos is not None else set()
        for name in ("<|im_end|>", "<|endoftext|>", "<|eot_id|>", "<|end_of_text|>", "<|eom_id|>"):
            if name in self.special:
                stops.add(self.special[name])
        return stops


class StreamDecoder:
    """Turns token ids into text incrementally without splitting multi-byte characters."""

    def __init__(self, tok: Tokenizer):
        self.tok, self.dec = tok, codecs.getincrementaldecoder("utf-8")(errors="replace")

    def push(self, tid: int) -> str:
        return self.dec.decode(self.tok.token_bytes(tid))


# ---------------------------------------------------------------------------- chat templates
def render_chat(arch: str, messages: list[dict], tools: list[dict] | None = None, think: bool = False) -> str:
    """Prompt text for a chat, matching each family's official template."""
    if arch == "qwen3":
        out = []
        sys = messages[0]["content"] if messages and messages[0]["role"] == "system" else None
        if tools:
            out.append("<|im_start|>system\n" + (sys + "\n\n" if sys else "")
                       + "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
                       "You are provided with function signatures within <tools></tools> XML tags:\n<tools>"
                       + "".join("\n" + json.dumps(t, ensure_ascii=False) for t in tools)
                       + "\n</tools>\n\nFor each function call, return a json object with function name and arguments "
                       "within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, "
                       "\"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n")
        elif sys is not None:
            out.append(f"<|im_start|>system\n{sys}<|im_end|>\n")
        prev = None
        for i, m in enumerate(messages):
            role = m["role"]
            if role == "system" and i == 0:
                prev = role
                continue
            if role in ("user", "system"):
                out.append(f"<|im_start|>{role}\n{m['content']}<|im_end|>\n")
            elif role == "assistant":
                content = m.get("content") or ""
                if "</think>" in content:
                    content = content.split("</think>")[-1].lstrip("\n")
                calls = "".join(("\n" if (j or content) else "") + "<tool_call>\n" + json.dumps(
                    {"name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}, ensure_ascii=False)
                    + "\n</tool_call>" for j, c in enumerate(m.get("tool_calls") or []))
                out.append(f"<|im_start|>assistant\n{content}{calls}<|im_end|>\n")
            elif role == "tool":
                nxt = messages[i + 1]["role"] if i + 1 < len(messages) else None
                out.append(("<|im_start|>user" if prev != "tool" else "") + f"\n<tool_response>\n{m['content']}\n</tool_response>"
                           + ("<|im_end|>\n" if nxt != "tool" else ""))
            prev = role
        out.append("<|im_start|>assistant\n" + ("" if think else "<think>\n\n</think>\n\n"))
        return "".join(out)
    if arch == "llama":
        out = ["<|begin_of_text|>"]
        for m in messages:
            role = "ipython" if m["role"] == "tool" else m["role"]
            out.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{(m.get('content') or '').strip()}<|eot_id|>")
        out.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
        return "".join(out)
    raise ValueError(f"no chat template for {arch!r}")


def _args(a):
    if isinstance(a, str):
        try:
            return json.loads(a)
        except ValueError:
            return a
    return a


class OutputParser:
    """Splits a model's raw output stream into answer text, visible reasoning and tool calls."""

    TAGS = ("<think>", "</think>", "<tool_call>", "</tool_call>")

    def __init__(self):
        self.buf, self.mode, self.call_buf, self.calls = "", "text", "", []

    def feed(self, text: str) -> list[dict]:
        self.buf += text
        events = []
        while self.buf:
            idx = min(((self.buf.find(t), t) for t in self.TAGS if self.buf.find(t) >= 0), default=(-1, None))
            if idx[0] < 0:
                # hold back a possible partial tag at the end
                keep = max((k for k in range(1, 13) if any(t.startswith(self.buf[-k:]) for t in self.TAGS)), default=0)
                emit, self.buf = (self.buf[:-keep], self.buf[-keep:]) if keep else (self.buf, "")
                events += self._emit(emit)
                break
            pos, tag = idx
            events += self._emit(self.buf[:pos])
            self.buf = self.buf[pos + len(tag):]
            if tag == "<think>":
                self.mode = "think"
            elif tag == "</think>":
                self.mode = "text"
                self.buf = self.buf.lstrip("\n")
            elif tag == "<tool_call>":
                self.mode, self.call_buf = "call", ""
            else:
                self._finish_call()
                self.mode = "text"
        return events

    def _emit(self, s: str) -> list[dict]:
        if not s:
            return []
        if self.mode == "think":
            return [{"reasoning": s}]
        if self.mode == "call":
            self.call_buf += s
            return []
        return [{"delta": s}]

    def _finish_call(self) -> None:
        try:
            data = json.loads(self.call_buf.strip())
            args = data.get("arguments", {})
            self.calls.append({"id": f"call_{len(self.calls)}", "type": "function",
                               "function": {"name": str(data["name"]), "arguments": args if isinstance(args, str) else json.dumps(args)}})
        except (ValueError, KeyError, TypeError, AttributeError):
            pass

    def flush(self) -> list[dict]:
        ev = self._emit(self.buf)
        self.buf = ""
        if self.mode == "call":
            self._finish_call()
        return ev

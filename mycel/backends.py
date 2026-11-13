"""Inference backends a node can serve from.

Backends yield *events*: ``{"delta": str}`` for answer text, ``{"reasoning": str}`` for a thinking
model's visible reasoning, and ``{"tool_calls": [...]}`` (OpenAI format) when the model asks to
call a tool. Tools are never executed on the serving node —
the tool call travels back to the requester, whose own node runs it (with the user's
approval) and sends the result in a follow-up request.

* ``OpenAICompatBackend`` — any local server speaking the OpenAI chat API with streaming:
  llama.cpp ``llama-server`` (use ``--jinja`` for tool calling), Ollama (``/v1``), vLLM,
  LM Studio. The node only talks to it on loopback and never exposes it.
* ``MockBackend`` — deterministic stand-in model for simulations and tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import AsyncIterator
from urllib.parse import urlparse

import aiohttp

from .netguard import is_loopback_host


class BackendError(Exception):
    pass


class Backend:
    models: list[str]
    tokens_per_s: float = 0.0

    async def start(self) -> None:  # measure throughput, open sessions
        pass

    async def close(self) -> None:
        pass

    def generate(self, model: str, messages: list[dict], max_tokens: int, temperature: float,
                 tools: list[dict] | None = None) -> AsyncIterator[dict]:
        raise NotImplementedError


# ---------------------------------------------------------------------------- mock model
_ARITH = re.compile(r"(?<![\w.])(\d[\d.,\s]*(?:\s*[-+*/^%x×÷]\s*\(?\s*\d[\d.,]*\s*\)?)+)")


def _tool_names(tools) -> dict[str, str]:
    """Map the plugin id prefix -> full tool name, e.g. 'calculator' -> 'calculator__evaluate'."""
    out = {}
    for t in tools or []:
        name = t.get("function", {}).get("name", "")
        out.setdefault(name.split("__")[0], name)
        out[name] = name
    return out


def _topic(text: str) -> str:
    words = re.findall(r"[\w'-]+", text)
    stop = {"what", "is", "the", "a", "an", "how", "do", "i", "can", "you", "me", "please", "tell", "about",
            "explain", "write", "of", "to", "in", "for", "and", "my", "with", "why", "does", "are"}
    keep = [w for w in words if w.lower() not in stop][:6]
    return " ".join(keep) or "that"


def _mock_reply(model: str, prompt: str, tool_result: str | None) -> str:
    p = prompt.lower()
    if tool_result is not None:
        return f"Here's the result:\n\n> {tool_result.strip()}\n\nWant me to do anything else with it?"
    topic = _topic(prompt)
    if any(k in p for k in ("code", "function", "python", "script", "implement")):
        return (f"Here's a compact starting point for **{topic}**:\n\n```python\n"
                "def solve(items):\n    \"\"\"Return the items sorted by score, highest first.\"\"\"\n"
                "    return sorted(items, key=lambda item: item.score, reverse=True)\n```\n\n"
                "A few notes:\n\n- It runs in `O(n log n)` time.\n- `sorted` returns a new list, so the input is untouched.\n"
                "- Swap the key function to rank by anything else.\n\n"
                f"_Simulated reply from `{model}` — connect a real model server to get genuine answers._")
    if any(k in p for k in ("explain", "what is", "what are", "how does", "why")):
        return (f"**{topic.capitalize()}**, in short:\n\n"
                "1. **The core idea** — break the problem into parts you can reason about independently.\n"
                "2. **How it works** — each part does one job and hands its result to the next.\n"
                "3. **Why it matters** — small, well-defined pieces are easier to test, share and improve.\n\n"
                "| Aspect | What to look for |\n|---|---|\n| Inputs | What it needs to start |\n"
                "| Outputs | What it produces |\n| Limits | Where it breaks down |\n\n"
                f"_Simulated reply from `{model}` — connect a real model server to get genuine answers._")
    if model.endswith("@swarm"):
        return (f"Happy to help with **{topic}**.\n\n"
                "This reply was computed by a *swarm*: several members each ran a slice of the model's layers, in order, "
                "while your device kept the words — it turned your message into numbers, passed them down the chain, "
                "and turned the result back into text.\n\n"
                "- Tap **Path** below to see who computed which layers.\n"
                "- Try a calculation like *what is 1499 * 12 / 7* to see plugins in action.\n\n"
                f"_Simulated reply from `{model[:-6]}` running as a simulated swarm._")
    return (f"Happy to help with **{topic}**.\n\n"
            "This mesh routed your message to a volunteer node, which streamed this reply back with a signed "
            "receipt so you can see exactly who served it.\n\n"
            "- Ask me to **explain** something, or to **write code**.\n"
            "- Try a calculation like *what is 1499 * 12 / 7* to see plugins in action.\n\n"
            f"_Simulated reply from `{model}` — connect a real model server to get genuine answers._")


def _mock_tool_call(prompt: str, tools) -> dict | None:
    names = _tool_names(tools)
    p = prompt.lower()
    m = _ARITH.search(prompt)
    if m and "calculator" in names:
        expr = m.group(1).replace("×", "*").replace("x", "*").replace("÷", "/").replace(",", "").strip()
        return {"name": names["calculator"], "arguments": {"expression": expr}}
    if "clock" in names and re.search(r"\b(time|date|day is it|today)\b", p):
        return {"name": names["clock"], "arguments": {}}
    if "mesh" in names and re.search(r"\b(capacity|how many (nodes|members)|mesh (status|stats)|nodes? (are )?online)\b", p):
        return {"name": names["mesh"], "arguments": {}}
    if "notes" in names and re.search(r"\b(remember|note that|save a note)\b", p):
        text = re.sub(r"(?i).*?\b(remember|note that|save a note)\b[:\s]*", "", prompt, count=1).strip() or prompt
        return {"name": names["notes"], "arguments": {"text": text}}
    return None


class MockBackend(Backend):
    def __init__(self, models: list[str], tokens_per_s: float = 40.0, fail: bool = False):
        self.models = list(models)
        self.tokens_per_s = tokens_per_s
        self.fail = fail

    async def generate(self, model, messages, max_tokens, temperature, tools=None):
        if self.fail:
            raise BackendError("mock backend configured to fail")
        last = messages[-1] if messages else {"role": "user", "content": ""}
        user = next((m.get("content") or "" for m in reversed(messages) if m["role"] == "user"), "")
        if last["role"] == "user" and tools:
            call = _mock_tool_call(user, tools)
            if call:
                await asyncio.sleep(0.25)
                cid = "call_" + hashlib.sha256((user + call["name"]).encode()).hexdigest()[:12]
                yield {"tool_calls": [{"id": cid, "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"])}}]}
                return
        tool_result = last.get("content") if last["role"] == "tool" else None
        if "think" in user.lower():  # lets tests exercise reasoning across the mesh
            for piece in ["Let", " me", " think", " this", " through."]:
                await asyncio.sleep(1.0 / self.tokens_per_s)
                yield {"reasoning": piece}
        text = _mock_reply(model, user, tool_result)
        delay = 1.0 / self.tokens_per_s
        for i, piece in enumerate(re.findall(r"\s*\S+", text)[:max_tokens]):
            await asyncio.sleep(delay)
            yield {"delta": piece}


# ---------------------------------------------------------------------------- real servers
class OpenAICompatBackend(Backend):
    def __init__(self, base_url: str, models: list[str] | None = None, api_key_env: str | None = None):
        host = urlparse(base_url).hostname or ""
        if not is_loopback_host(host):
            raise ValueError("backend base_url must be a loopback address (the node fronts the model server)")
        self.base_url = base_url.rstrip("/")
        self.models = list(models or [])
        self._api_key_env = api_key_env
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict:
        key = os.environ.get(self._api_key_env) if self._api_key_env else None
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(headers=self._headers())
        if not self.models:
            async with self._session.get(f"{self.base_url}/v1/models") as r:
                r.raise_for_status()
                self.models = [m["id"] for m in (await r.json()).get("data", [])]
        if not self.models:
            raise BackendError("backend reports no models")
        n, first = 0, None
        async for ev in self.generate(self.models[0], [{"role": "user", "content": "Count from 1 to 40."}], 48, 0.0):
            if "delta" in ev or "reasoning" in ev:  # thinking models spend their first tokens reasoning
                n += 1
                first = first or time.perf_counter()
        if n < 4 or first is None:
            raise BackendError("throughput probe produced too few tokens")
        self.tokens_per_s = round((n - 1) / max(1e-6, time.perf_counter() - first), 1)

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def generate(self, model, messages, max_tokens, temperature, tools=None):
        if self._session is None:
            raise BackendError("backend not started")
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": True}
        if tools:
            payload["tools"] = tools
        calls: dict[int, dict] = {}
        async with self._session.post(f"{self.base_url}/v1/chat/completions", json=payload) as r:
            if r.status != 200:
                raise BackendError(f"backend HTTP {r.status}")
            async for raw in r.content:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("reasoning_content"):
                    yield {"reasoning": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"delta": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                 "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
        if calls:
            yield {"tool_calls": [calls[i] for i in sorted(calls)]}

"""First-party plugins that run in-process. No network access, no shell."""
from __future__ import annotations

import ast
import json
import math
import operator
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Plugin:
    id = ""
    tools_spec: list[dict] = []

    async def tools(self) -> list[dict]:
        return self.tools_spec

    async def call(self, tool: str, args: dict) -> str:
        raise NotImplementedError

    async def stop(self) -> None:
        pass


# ------------------------------------------------------------------ calculator
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "log", "log10",
                                        "log2", "exp", "floor", "ceil", "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round)
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr: str) -> float | int:
    if len(expr) > 200:
        raise ValueError("expression too long")
    tree = ast.parse(expr.replace("^", "**"), mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e6):
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
            return _UN[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
                and not node.keywords and len(node.args) <= 2):
            args = [ev(a) for a in node.args]
            if node.func.id == "factorial" and args and args[0] > 500:
                raise ValueError("factorial argument too large")
            return _FUNCS[node.func.id](*args)
        raise ValueError("unsupported expression")

    return ev(tree)


class Calculator(Plugin):
    id = "calculator"
    tools_spec = [{"name": "evaluate", "description": "Evaluate an arithmetic expression exactly. Supports + - * / // % ^, parentheses, sqrt, log, sin, cos, pi, e.",
                   "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}]

    async def call(self, tool, args):
        expr = str(args.get("expression", ""))
        value = safe_eval(expr)
        if isinstance(value, float):
            value = round(value, 10)
            if value.is_integer():
                value = int(value)
        if isinstance(value, int):
            return f"{expr} = {value:,}"
        if abs(value) >= 1e15 or (value and abs(value) < 1e-6):
            return f"{expr} = {value:.10g}"
        return f"{expr} = " + f"{value:,.6f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------ clock
class Clock(Plugin):
    id = "clock"
    tools_spec = [{"name": "now", "description": "Current date and time, optionally in an IANA time zone like 'Africa/Lagos'.",
                   "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}}}]

    async def call(self, tool, args):
        tz_name = args.get("timezone")
        try:
            tz = ZoneInfo(tz_name) if tz_name else None
        except (ZoneInfoNotFoundError, ValueError):
            return f"Unknown time zone {tz_name!r}"
        now = datetime.now(tz).astimezone(tz)
        return now.strftime("%A %d %B %Y, %H:%M %Z").strip()


# ------------------------------------------------------------------ unit converter
_UNITS = {
    "length": {"mm": 1e-3, "cm": 1e-2, "m": 1, "km": 1e3, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344},
    "mass": {"mg": 1e-6, "g": 1e-3, "kg": 1, "t": 1e3, "oz": 0.028349523125, "lb": 0.45359237},
    "data": {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4},
    "time": {"ms": 1e-3, "s": 1, "min": 60, "h": 3600, "day": 86400, "week": 604800},
    "speed": {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444},
}


class Units(Plugin):
    id = "units"
    tools_spec = [{"name": "convert", "description": "Convert a value between units of length, mass, data size, time, speed or temperature (c, f, k).",
                   "parameters": {"type": "object", "properties": {"value": {"type": "number"}, "from": {"type": "string"}, "to": {"type": "string"}},
                                  "required": ["value", "from", "to"]}}]

    async def call(self, tool, args):
        v, a, b = float(args["value"]), str(args["from"]).lower(), str(args["to"]).lower()
        temps = {"c", "f", "k"}
        if a in temps and b in temps:
            c = v if a == "c" else (v - 32) * 5 / 9 if a == "f" else v - 273.15
            out = c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15
            return f"{v:g} °{a.upper()} = {out:.4g} °{b.upper()}"
        for table in _UNITS.values():
            if a in table and b in table:
                return f"{v:g} {a} = {v * table[a] / table[b]:.6g} {b}"
        return f"Can't convert {a} to {b}"


# ------------------------------------------------------------------ mesh status
class MeshStatus(Plugin):
    id = "mesh"
    tools_spec = [{"name": "status", "description": "Live statistics about the Mycel network: nodes, capacity, models.",
                   "parameters": {"type": "object", "properties": {}}}]

    def __init__(self, stats_fn):
        self._stats = stats_fn

    async def call(self, tool, args):
        s = self._stats()
        models = ", ".join(f"{m} ({c} nodes)" for m, c in sorted(s["models"].items(), key=lambda x: -x[1])) or "none"
        return (f"{s['active_nodes']} active nodes, {s['serving_nodes']} serving, "
                f"{s['sustained_tokens_per_s']} tokens/s of donated capacity, {s['accel_memory_gb']} GB GPU memory. Models: {models}.")


# ------------------------------------------------------------------ notes
class Notes(Plugin):
    id = "notes"
    MAX_NOTES, MAX_LEN = 2000, 4000
    tools_spec = [
        {"name": "add", "description": "Save a note on the user's device.",
         "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "search", "description": "Search the user's saved notes (empty query lists recent notes).",
         "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}},
    ]

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return []

    async def call(self, tool, args):
        notes = self._load()
        if tool == "add":
            text = str(args.get("text", "")).strip()[: self.MAX_LEN]
            if not text:
                return "Nothing to save."
            notes.append({"id": uuid.uuid4().hex[:8], "text": text, "at": time.time()})
            notes = notes[-self.MAX_NOTES:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(notes))
            os.replace(tmp, self.path)
            return f"Saved note: “{text}”"
        q = str(args.get("query", "")).lower()
        hits = [n for n in reversed(notes) if q in n["text"].lower()][:10]
        return "\n".join(f"- {n['text']}" for n in hits) if hits else "No matching notes."

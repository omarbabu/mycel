"""Minimal Model Context Protocol client over stdio (JSON-RPC 2.0, newline-delimited).

An MCP server is a local program. We start it with an explicit argv (never a shell), a
scrubbed environment (no inherited secrets such as cloud credentials or API keys), and a
dedicated working directory. Only the ``tools`` capability is used.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
from pathlib import Path

from .builtins import Plugin

PROTOCOL_VERSION = "2025-06-18"
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL")


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, argv: list[str], cwd: Path, extra_env: dict[str, str] | None = None, timeout_s: float = 30.0):
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise MCPError("invalid command")
        self.argv, self.cwd, self.timeout_s = argv, cwd, timeout_s
        self.env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ} | (extra_env or {})
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task] = []
        self.server_info: dict = {}

    async def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(self.cwd), env=self.env, limit=8 << 20)
        except (FileNotFoundError, PermissionError) as exc:
            raise MCPError(f"can't start {self.argv[0]!r}: {exc.strerror or exc}") from exc
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        result = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "mycel", "version": "0.2.0"}})
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _send(self, msg: dict) -> None:
        if not self.proc or self.proc.returncode is not None:
            raise MCPError("plugin process is not running")
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None):
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise MCPError(f"{method} timed out") from exc
        finally:
            self._pending.pop(rid, None)

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # servers sometimes log to stdout; ignore non-JSON lines
            if not isinstance(msg, dict) or "id" not in msg or "method" in msg:
                continue  # notifications / server->client requests are not supported
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                if "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {}
                    fut.set_exception(MCPError(str(err.get("message", "plugin error"))))
                else:
                    fut.set_result(msg.get("result"))
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("plugin exited: " + " | ".join(self.stderr_tail)))

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").rstrip()[:300])

    async def list_tools(self) -> list[dict]:
        result = await self.request("tools/list")
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            return "", True
        parts = []
        for c in result.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type', 'content')} omitted]")
        return "\n".join(parts), bool(result.get("isError"))

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for t in self._tasks:
            t.cancel()


class MCPPlugin(Plugin):
    def __init__(self, plugin_id: str, argv: list[str], cwd: Path, env: dict[str, str] | None = None):
        self.id = plugin_id
        self._spec = (argv, cwd, env)
        self.client = MCPClient(argv, cwd, env)
        self._tools: list[dict] | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        async with self._lock:
            if self.client.proc is None or self.client.proc.returncode is not None:
                if self.client.proc is not None:  # restart after a crash
                    self.client = MCPClient(*self._spec)
                await self.client.start()
                self._tools = None
            if self._tools is None:
                self._tools = [{"name": t["name"], "description": str(t.get("description", ""))[:1000],
                                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}
                               for t in await self.client.list_tools()]

    async def tools(self) -> list[dict]:
        await self._ensure()
        return self._tools or []

    async def call(self, tool: str, args: dict) -> str:
        await self._ensure()
        text, is_error = await self.client.call_tool(tool, args)
        return ("Error: " if is_error else "") + text

    async def stop(self) -> None:
        await self.client.stop()

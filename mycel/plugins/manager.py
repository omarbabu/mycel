"""Plugin marketplace catalog, install state and tool dispatch."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from .builtins import Calculator, Clock, MeshStatus, Notes, Plugin, Units
from .mcp import MCPError, MCPPlugin

MAX_RESULT_CHARS = 16_000
_TOOL_RE = re.compile(r"[^a-zA-Z0-9_-]")


class PluginError(Exception):
    pass


def _entry(id, name, tagline, category, icon, permissions, *, publisher="Mycel", kind="builtin",
           verified=True, featured=False, default_permission="auto", command=None, config=None, description=""):
    return {"id": id, "name": name, "tagline": tagline, "category": category, "icon": icon,
            "permissions": permissions, "publisher": publisher, "kind": kind, "verified": verified,
            "featured": featured, "default_permission": default_permission, "command": command,
            "config": config or [], "description": description or tagline}


MCP_PUBLISHER = "Model Context Protocol · reference server"
CATALOG = [
    _entry("calculator", "Calculator", "Exact arithmetic, so answers never guess at numbers.", "Utilities", "calculator",
           ["Runs math on this device. No network, no files."], featured=True),
    _entry("clock", "Clock", "Current date and time in any time zone.", "Utilities", "clock",
           ["Reads this device's clock."]),
    _entry("units", "Unit Converter", "Length, mass, data sizes, speed, time and temperature.", "Utilities", "ruler",
           ["Pure computation on this device."]),
    _entry("mesh", "Mesh Insights", "Let the assistant answer questions about the live network.", "Mycel", "mesh",
           ["Reads public mesh statistics your node already sees."], featured=True),
    _entry("notes", "Notes", "Ask the assistant to remember things. Notes stay on this device.", "Productivity", "note",
           ["Saves and searches notes in your Mycel folder.",
            "Note text you retrieve is sent to the node answering your chat."],
           default_permission="ask", featured=True),
    _entry("fetch", "Web Fetch", "Read web pages and turn them into clean text for the model.", "Web", "globe",
           ["Runs a program on this device: uvx mcp-server-fetch", "Fetches any URL the model asks for (you approve each one)."],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="ask", featured=True,
           command=["uvx", "mcp-server-fetch"]),
    _entry("filesystem", "Files", "Let the assistant read and edit files in one folder you choose.", "Files", "folder",
           ["Runs a program on this device: npx @modelcontextprotocol/server-filesystem",
            "Reads and writes files inside the folder you choose — nowhere else."],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="ask",
           command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "{folder}"],
           config=[{"key": "folder", "label": "Folder the assistant may access", "type": "directory"}]),
    _entry("git", "Git", "Inspect history, diffs and branches of a repository.", "Developer", "branch",
           ["Runs a program on this device: uvx mcp-server-git", "Reads the repository you choose and can create commits."],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="ask",
           command=["uvx", "mcp-server-git", "--repository", "{repository}"],
           config=[{"key": "repository", "label": "Repository folder", "type": "directory"}]),
    _entry("memory", "Knowledge Graph", "Long-term memory of people, projects and facts across chats.", "Productivity", "graph",
           ["Runs a program on this device: npx @modelcontextprotocol/server-memory", "Stores a knowledge graph on this device."],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="ask",
           command=["npx", "-y", "@modelcontextprotocol/server-memory"]),
    _entry("time", "World Time", "Time zone conversions and scheduling across regions.", "Utilities", "globe-clock",
           ["Runs a program on this device: uvx mcp-server-time"],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="auto",
           command=["uvx", "mcp-server-time"]),
    _entry("sequential-thinking", "Deep Reasoning", "Structured step-by-step thinking for hard problems.", "Reasoning", "spark",
           ["Runs a program on this device: npx @modelcontextprotocol/server-sequential-thinking"],
           publisher=MCP_PUBLISHER, kind="mcp", default_permission="auto",
           command=["npx", "-y", "@modelcontextprotocol/server-sequential-thinking"]),
]
CATALOG_BY_ID = {e["id"]: e for e in CATALOG}
DEFAULT_INSTALLED = {"calculator": "auto", "clock": "auto", "units": "auto", "mesh": "auto", "notes": "ask"}


def _validate_dir(path: str) -> str:
    p = Path(os.path.expanduser(str(path))).resolve()
    if not p.is_absolute() or not p.is_dir():
        raise PluginError(f"{path} is not an existing folder")
    if p == Path("/") or p == Path.home():
        raise PluginError("pick a specific folder, not your whole disk or home folder")
    return str(p)


class PluginManager:
    def __init__(self, data_dir: Path, stats_fn):
        self.dir = Path(data_dir)
        self.state_path = self.dir / "plugins.json"
        self._stats_fn = stats_fn
        self._live: dict[str, Plugin] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}
        self.state = self._load()

    # ------------------------------------------------------------ state
    def _load(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
            if isinstance(state, dict) and isinstance(state.get("installed"), dict):
                state.setdefault("custom", {})
                return state
        except (FileNotFoundError, ValueError):
            pass
        return {"installed": {pid: {"enabled": True, "permission": perm, "config": {}}
                              for pid, perm in DEFAULT_INSTALLED.items()}, "custom": {}}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_path)

    def _entry(self, pid: str) -> dict:
        if pid in CATALOG_BY_ID:
            return CATALOG_BY_ID[pid]
        if pid in self.state["custom"]:
            c = self.state["custom"][pid]
            return _entry(pid, c["name"], "Custom MCP server you added.", "Custom", "plug",
                          [f"Runs a program on this device: {' '.join(c['command'])}"],
                          publisher="You · unverified", kind="mcp", verified=False, default_permission="ask",
                          command=c["command"])
        raise PluginError(f"unknown plugin {pid!r}")

    def catalog(self) -> list[dict]:
        out = []
        for pid in [*CATALOG_BY_ID, *self.state["custom"]]:
            e = dict(self._entry(pid))
            inst = self.state["installed"].get(pid)
            e.update(installed=inst is not None, enabled=bool(inst and inst["enabled"]),
                     permission=(inst or {}).get("permission", e["default_permission"]),
                     settings=(inst or {}).get("config", {}))
            out.append(e)
        return out

    def resolved_command(self, pid: str, config: dict) -> list[str] | None:
        e = self._entry(pid)
        if e["kind"] != "mcp":
            return None
        values = {}
        for field in e["config"]:
            if field["key"] not in config:
                raise PluginError(f"{field['label']} is required")
            values[field["key"]] = _validate_dir(config[field["key"]]) if field["type"] == "directory" else str(config[field["key"]])
        return [values.get(a[1:-1], a) if a.startswith("{") and a.endswith("}") else a for a in e["command"]]

    # ------------------------------------------------------------ lifecycle
    async def install(self, pid: str, config: dict | None = None, confirm_command: list[str] | None = None) -> dict:
        e = self._entry(pid)
        config = dict(config or {})
        cmd = self.resolved_command(pid, config)
        if cmd is not None:
            # A program will run on this device: the caller must echo back exactly what it approved.
            if confirm_command != cmd:
                raise PluginError("confirmation required")
            config = {k: (_validate_dir(v) if any(f["key"] == k and f["type"] == "directory" for f in e["config"]) else v)
                      for k, v in config.items()}
        await self._stop(pid)
        self.state["installed"][pid] = {"enabled": True, "permission": e["default_permission"], "config": config}
        self._save()
        return {"id": pid, "command": cmd}

    async def add_custom(self, name: str, command: list[str], confirm: bool) -> str:
        if not confirm:
            raise PluginError("confirmation required")
        if not name.strip() or not command or not all(isinstance(a, str) and a for a in command) or len(command) > 32:
            raise PluginError("give the plugin a name and a command")
        pid = "custom-" + secrets.token_hex(3)
        self.state["custom"][pid] = {"name": name.strip()[:40], "command": command}
        self.state["installed"][pid] = {"enabled": True, "permission": "ask", "config": {}}
        self._save()
        return pid

    async def uninstall(self, pid: str) -> None:
        self._entry(pid)
        await self._stop(pid)
        self.state["installed"].pop(pid, None)
        self.state["custom"].pop(pid, None)
        self._save()

    async def update(self, pid: str, enabled: bool | None = None, permission: str | None = None) -> None:
        inst = self.state["installed"].get(pid)
        if inst is None:
            raise PluginError("plugin is not installed")
        if permission is not None:
            if permission not in ("ask", "auto"):
                raise PluginError("permission must be 'ask' or 'auto'")
            inst["permission"] = permission
        if enabled is not None:
            inst["enabled"] = bool(enabled)
            if not enabled:
                await self._stop(pid)
        self._save()

    async def _stop(self, pid: str) -> None:
        live = self._live.pop(pid, None)
        if live:
            await live.stop()

    async def close(self) -> None:
        for pid in list(self._live):
            await self._stop(pid)

    def _instance(self, pid: str) -> Plugin:
        if pid in self._live:
            return self._live[pid]
        inst = self.state["installed"][pid]
        builtin = {"calculator": Calculator, "clock": Clock, "units": Units}
        if pid in builtin:
            plugin: Plugin = builtin[pid]()
        elif pid == "mesh":
            plugin = MeshStatus(self._stats_fn)
        elif pid == "notes":
            plugin = Notes(self.dir / "notes.json")
        else:
            plugin = MCPPlugin(pid, self.resolved_command(pid, inst["config"]), self.dir / "plugin-data" / pid)
        self._live[pid] = plugin
        return plugin

    # ------------------------------------------------------------ tools
    async def tools(self) -> list[dict]:
        """Tools from enabled plugins, in OpenAI format, plus UI metadata."""
        out, self._tool_map = [], {}
        for pid, inst in self.state["installed"].items():
            if not inst["enabled"]:
                continue
            e = self._entry(pid)
            try:
                specs = await self._instance(pid).tools()
            except (MCPError, PluginError, OSError) as exc:
                out.append({"plugin": pid, "error": str(exc)})
                continue
            for spec in specs:
                full = _TOOL_RE.sub("_", f"{pid}__{spec['name']}")[:64]
                self._tool_map[full] = (pid, spec["name"])
                out.append({"plugin": pid, "plugin_name": e["name"], "icon": e["icon"], "tool": spec["name"],
                            "permission": inst["permission"],
                            "spec": {"type": "function", "function": {
                                "name": full, "description": spec.get("description", ""),
                                "parameters": spec.get("parameters") or {"type": "object", "properties": {}}}}})
        return out

    async def call(self, full_name: str, arguments, approved: bool = False) -> dict:
        if full_name not in self._tool_map:
            await self.tools()
        if full_name not in self._tool_map:
            raise PluginError(f"no enabled plugin provides {full_name!r}")
        pid, tool = self._tool_map[full_name]
        if self.state["installed"][pid]["permission"] == "ask" and not approved:
            raise PluginError("this tool needs your approval for each use")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError as exc:
                raise PluginError("tool arguments are not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise PluginError("tool arguments must be an object")
        try:
            result = await self._instance(pid).call(tool, arguments)
            ok = True
        except (MCPError, ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError, ArithmeticError) as exc:
            result, ok = f"Error: {exc}", False
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n…[truncated]"
        return {"plugin": pid, "tool": tool, "ok": ok, "result": result}

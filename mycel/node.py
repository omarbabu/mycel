"""A mesh node: members-only peer protocol, gossip, routing, serving and split (swarm) inference.

Every node-to-node message is a signed envelope sealed end-to-end to the recipient (``rpc.py``)
and carried by a transport (``transport.py``): direct HTTP on a mesh listener, or outbound-only
relays for nodes behind NAT. The loopback control plane (app UI, local APIs) lives in ``api.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import random
import secrets
import time
from pathlib import Path
from typing import AsyncIterator

from aiohttp import web

from . import __version__
from . import models as catalog
from .backends import Backend, BackendError, MockBackend, OpenAICompatBackend
from .config import NodeConfig
from .governor import Governor, Rejected
from .hardware import HardwareProfile, check_serving_eligibility, probe
from .identity import EnvelopeError, Identity, ReplayCache, canonical, verify_direct, verify_relayed
from .location import decode_center, public_cell
from .membership import Membership, PeerRecord
from .models import ModelServer
from .netguard import is_loopback_host
from .rpc import KxKey, SealError, StreamOpener, StreamSealer, open_request, seal_request
from .store import Ledger, _atomic_write
from .relays import RelayBook
from .transport import HTTPTransport, Netem, PeerUnreachable, RelayTransport, relay_url_ok
from .trust import GLOBAL_MESH_ID, TrustError, TrustState, issue_root, mint_ticket, parse_membership_code

log = logging.getLogger("mycel")
MAX_ATTEMPTS = 3
MAX_PROMPT_CHARS = 200_000
MAX_TOOLS_BYTES = 64_000
SWARM_SUFFIX = "@swarm"
TIER_FLOOR_GB = {"T3": 48.0, "T2": 16.0, "T1": 8.0}


class RouteError(Exception):
    pass


class RemoteError(RouteError):
    """The peer answered, but with an error (status mirrors HTTP: 403, 404, 429, 503...)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}")
        self.status = status


class _Fail(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def split_model(model: str | None) -> tuple[str | None, str]:
    """'llama-3.3-70b@swarm' -> ('llama-3.3-70b', 'swarm'); plain ids route automatically."""
    if not model:
        return None, "auto"
    if model.endswith(SWARM_SUFFIX):
        return model[: -len(SWARM_SUFFIX)], "swarm"
    return model, "auto"


# ---------------------------------------------------------------------------- validation
def validate_messages(messages) -> list[dict]:
    if not isinstance(messages, list) or not messages or len(messages) > 500:
        raise ValueError("messages must be a non-empty list")
    out, total = [], 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant", "tool"):
            raise ValueError("each message needs a role of system, user, assistant or tool")
        role, content = m["role"], m.get("content")
        if isinstance(content, list):  # OpenAI content parts: keep the text parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        msg = {"role": role, "content": content if isinstance(content, str) else ""}
        if role == "assistant" and m.get("tool_calls"):
            calls = m["tool_calls"]
            if not isinstance(calls, list) or len(calls) > 16:
                raise ValueError("invalid tool_calls")
            msg["tool_calls"] = []
            for c in calls:
                fn = c.get("function") if isinstance(c, dict) else None
                if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
                    raise ValueError("invalid tool call")
                args = fn.get("arguments", "{}")
                msg["tool_calls"].append({"id": str(c.get("id", ""))[:64], "type": "function", "function": {
                    "name": fn["name"][:64], "arguments": args if isinstance(args, str) else json.dumps(args)}})
        elif role == "tool":
            msg["tool_call_id"] = str(m.get("tool_call_id", ""))[:64]
        elif not isinstance(content, str):
            raise ValueError(f"{role} messages need string content")
        total += len(msg["content"]) + len(json.dumps(msg.get("tool_calls", "")))
        out.append(msg)
    if total > MAX_PROMPT_CHARS:
        raise ValueError(f"conversation exceeds {MAX_PROMPT_CHARS:,} characters")
    return out


def validate_tools(tools) -> list[dict] | None:
    if not tools:
        return None
    if not isinstance(tools, list) or len(tools) > 64 or len(json.dumps(tools)) > MAX_TOOLS_BYTES:
        raise ValueError("too many or too large tool definitions")
    out = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if t.get("type") != "function" or not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise ValueError("tools must be OpenAI function definitions")
        params = fn.get("parameters")
        out.append({"type": "function", "function": {
            "name": fn["name"][:64], "description": str(fn.get("description", ""))[:2000],
            "parameters": params if isinstance(params, dict) else {"type": "object", "properties": {}}}})
    return out


def output_digest(text: str, tool_calls: list | None, reasoning: str = "") -> str:
    body = {"text": text, "tool_calls": tool_calls or []}
    if reasoning:
        body["reasoning"] = reasoning
    return hashlib.sha256(canonical(body)).hexdigest()


def parse_peer(entry: str) -> tuple[str, str]:
    """'<node_id>@<ip>:<port>' -> (node_id, addr)."""
    node_id, sep, addr = entry.partition("@")
    if not sep or not node_id or ":" not in addr:
        raise ValueError(f"peer must look like <node-id>@<ip>:<port>, got {entry!r}")
    return node_id, addr


def _bad_dial_target(host: str, loopback_mesh: bool) -> bool:
    """Refuse to dial addresses a malicious advert could use to pivot into local services."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True  # adverts must carry literal IPs; no DNS lookups on peer-supplied names
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if ip.is_loopback:
        return not loopback_mesh  # loopback peers only make sense in a single-machine mesh
    return loopback_mesh  # a loopback-only node never dials out to other machines


# ---------------------------------------------------------------------------- node
class Node:
    def __init__(self, cfg: NodeConfig, identity: Identity | None = None, backend: Backend | None = None):
        from .swarm.client import SwarmClient
        from .swarm.host import SwarmHost

        self.cfg = cfg.validate()
        self.data_dir = Path(cfg.data_dir).expanduser() / cfg.name
        self.identity = identity or Identity.load_or_create(cfg.key_path)
        self.id = self.identity.node_id
        self.kx = KxKey.load_or_create(self.data_dir / "kx_key")
        self._load_settings()
        self.backend = backend or self._make_backend()
        self.models = ModelServer(self.data_dir / "models", cfg.backend.server_cmd)
        self.governor = Governor(cfg.limits)
        self.trust, self.bootstrap = self._load_membership()
        self.membership = Membership(self.id, cfg.mesh.advert_ttl_s, self.trust)
        self.replay = ReplayCache()
        self.ledger = Ledger(self.data_dir / "ledger.json")
        self.internet = cfg.mesh.internet
        self.loopback_mesh = is_loopback_host(cfg.mesh.bind_host) and not self.internet
        # Internet mode picks relays from a persisted book (yours, bundled seeds, learned);
        # simulations and tests use exactly the configured (loopback) relays.
        self.relay_book = RelayBook(self.data_dir, lambda u: relay_url_ok(u, self.loopback_mesh),
                                    cfg.mesh.relays) if self.internet else None
        self.hw: HardwareProfile | None = None
        self.eligibility: list[str] = []
        self.serving = False
        self.mesh_port = cfg.mesh.port
        self.control_port = cfg.control_port
        self.started_at = time.time()
        self.counters = {"routed_ok": 0, "routed_failed": 0, "served_local": 0, "served_mesh": 0,
                         "tokens_served_mesh": 0, "tokens_from_mesh": 0, "swarm_replies": 0, "stage_forwards": 0}
        self.netem: Netem | None = None    # simulations inject WAN conditions here before start()
        self.http = HTTPTransport(self) if cfg.mesh.listen else None
        self.relay: RelayTransport | None = None
        self._stopping = False
        self._contacts: dict[str, str] = {}   # node id -> verified encryption key
        self._seq = 0
        self._runners: list[web.AppRunner] = []
        self._tasks: list[asyncio.Task] = []
        self._gossip_seen: dict[str, list[float]] = {}
        self._advert_cache: tuple | None = None
        self.swarm_host = SwarmHost(self)
        self.swarm_client = SwarmClient(self)
        self.api = None  # set in start(); see api.py

    # ------------------------------------------------------------------ membership
    @property
    def _membership_path(self) -> Path:
        return self.data_dir / "membership.json"

    def _load_membership(self) -> tuple[TrustState, list[str]]:
        try:
            data = json.loads(self._membership_path.read_text())
            return TrustState.from_json(data), list(data.get("bootstrap", [])) + list(self.cfg.mesh.bootstrap)
        except (FileNotFoundError, ValueError):
            return TrustState(), list(self.cfg.mesh.bootstrap)

    def _save_membership(self) -> None:
        _atomic_write(self._membership_path, {**self.trust.to_json(), "bootstrap": self.bootstrap[:16]})

    async def ensure_ticket(self) -> dict:
        """This device's proof-of-work identity ticket (relays require one from everyone)."""
        if self.trust.ticket is None or self.trust.ticket.get("pubkey") != self.identity.pubkey:
            self.trust.ticket = await asyncio.to_thread(mint_ticket, self.identity)
            self._save_membership()
        return self.trust.ticket

    async def join_global(self) -> None:
        """Join the founderless global mesh. Mints this device's identity ticket once (~1 s)."""
        await self.ensure_ticket()
        self.trust.mesh_id, self.trust.chain, self.trust.revocation = GLOBAL_MESH_ID, [], None
        self.bootstrap = list(dict.fromkeys(self.cfg.mesh.global_bootstrap + self.cfg.mesh.bootstrap))
        self.membership.peers.clear()
        self._save_membership()
        await self.connect_relays()

    # ------------------------------------------------------------------ internet relays
    def relay_urls(self) -> list[str]:
        return self.relay_book.home_set() if self.relay_book else list(self.cfg.mesh.relays)

    async def connect_relays(self) -> None:
        """Open outbound relay connections. In internet mode this happens only once you're a
        member: a node that hasn't joined anything never contacts a relay (or reveals its IP)."""
        if self.relay is not None or self._stopping:
            return
        if self.internet and not self.trust.is_member:
            return
        urls = self.relay_urls()
        if not urls:
            return
        self.relay = RelayTransport(self, urls, self.netem, book=self.relay_book)
        await self.relay.start()

    async def disconnect_relays(self) -> None:
        relay, self.relay = self.relay, None
        if relay is not None:
            await relay.stop()

    async def add_relay(self, url: str) -> None:
        if not self.relay_book:
            raise ValueError("this node isn't in internet mode")
        self.relay_book.add_user(url)
        if self.relay is None:
            await self.connect_relays()
        else:
            self.relay.add_home(url)

    async def remove_relay(self, url: str) -> None:
        if not self.relay_book:
            raise ValueError("this node isn't in internet mode")
        self.relay_book.remove_user(url)
        if self.relay is not None:
            await self.relay.remove_home(url)

    def network_view(self) -> dict:
        """What the app shows about connectivity (local only; never advertised)."""
        up = self.relay.connected_relays() if self.relay else []
        view = {"mode": "internet" if self.internet else "local", "listening": self.http is not None,
                "loopback_only": self.loopback_mesh, "relays": up, "online": any(r["up"] for r in up)}
        if self.relay_book:
            src = self.relay_book.sources()
            home = {r["url"]: r for r in up}
            view["known"] = [{"url": u, "source": s, "home": u in home, "up": bool(home.get(u, {}).get("up")),
                              "region": home.get(u, {}).get("region", "")} for u, s in src.items()]
            view["use_bundled"] = self.relay_book.use_bundled
            view["has_seeds"] = any(s == "bundled" for s in src.values())
        return view

    def create_private_mesh(self) -> None:
        """Advanced: found an invitation-only mesh (this node becomes its founder)."""
        self.trust.mesh_id = self.id
        self.trust.chain = [issue_root(self.identity)]
        self.trust.revocation = None
        self.bootstrap = []
        self.membership.peers.clear()
        self._save_membership()

    def join_private(self, code: str) -> None:
        mesh_id, chain, bootstrap = parse_membership_code(code, self.identity)
        self.trust.mesh_id, self.trust.chain, self.trust.revocation = mesh_id, chain, None
        self.bootstrap = bootstrap
        self.membership.peers.clear()
        self._save_membership()
        self._spawn(self.connect_relays())

    def leave_mesh(self) -> None:
        self.trust.mesh_id, self.trust.chain, self.trust.revocation = None, [], None  # ticket is kept
        self.membership.peers.clear()
        self.bootstrap = []
        self._save_membership()
        if self.internet:  # not a member any more: stop talking to relays at all
            self._spawn(self.disconnect_relays())

    def _spawn(self, coro) -> None:
        try:
            self._tasks.append(asyncio.get_running_loop().create_task(coro))
        except RuntimeError:  # no loop (sync callers in tests): nothing to connect yet
            coro.close()

    def revoke(self, node_id: str) -> None:
        self.trust.revoke(self.identity, node_id)
        self.membership.apply_revocations()
        self._save_membership()

    def block(self, node_id: str) -> None:
        if node_id == self.id:
            raise TrustError("you can't block yourself")
        self.trust.blocked.add(node_id)
        self.membership.apply_revocations()
        self._save_membership()

    def peer_entry(self) -> str:
        return f"{self.id}@{self.advertise_addr}"

    # ------------------------------------------------------------------ persisted settings
    @property
    def _settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def _load_settings(self) -> None:
        self.serve_model_id: str | None = None
        self.measured_bandwidth: dict | None = None
        try:
            data = json.loads(self._settings_path.read_text())
        except (FileNotFoundError, ValueError):
            return
        from dataclasses import fields
        lim = data.get("limits") or {}
        for f in fields(self.cfg.limits):
            if f.name in lim:
                setattr(self.cfg.limits, f.name, lim[f.name])
        loc = data.get("location") or {}
        for k in ("share", "lat", "lon", "precision"):
            if k in loc:
                setattr(self.cfg.location, k, loc[k])
        vis = data.get("visibility") or {}
        for f in fields(self.cfg.visibility):
            if isinstance(vis.get(f.name), bool):
                setattr(self.cfg.visibility, f.name, vis[f.name])
        sw = data.get("swarm") or {}
        if "model" in sw and (sw["model"] is None or sw["model"] in catalog.BY_ID):
            self.cfg.swarm.model = sw["model"]
        if isinstance(sw.get("layers"), int) and sw["layers"] >= 0:
            self.cfg.swarm.layers = sw["layers"]
        if isinstance(sw.get("budget_gb"), (int, float)) and sw["budget_gb"] >= 0:
            self.cfg.swarm.budget_gb = float(sw["budget_gb"])
        self.serve_model_id = data.get("serve_model") if data.get("serve_model") in catalog.BY_ID else None
        mb = data.get("measured_bandwidth")
        self.measured_bandwidth = mb if isinstance(mb, dict) and isinstance(mb.get("gbs"), (int, float)) else None
        try:
            self.cfg.validate()
        except ValueError:
            log.warning("ignoring invalid saved settings")

    def save_settings(self) -> None:
        loc, vis, sw = self.cfg.location, self.cfg.visibility, self.cfg.swarm
        _atomic_write(self._settings_path, {
            "limits": self.governor.limits.__dict__,
            "location": {"share": loc.share, "lat": loc.lat, "lon": loc.lon, "precision": loc.precision},
            "visibility": vis.__dict__,
            "swarm": {"model": sw.model, "budget_gb": sw.budget_gb, "layers": sw.layers},
            "serve_model": self.serve_model_id,
            "measured_bandwidth": self.measured_bandwidth,
        })

    # ------------------------------------------------------------------ serving a model
    def _eligibility_for(self, backend: Backend | None) -> list[str]:
        if not self.cfg.serve:
            return ["sharing is turned off on this device"]
        if backend is None:
            return ["no model is being served yet — pick one in Models"]
        return check_serving_eligibility(self.hw, backend.tokens_per_s)

    async def set_backend(self, backend: Backend | None) -> None:
        old, self.backend = self.backend, backend
        if old is not None and old is not backend:
            await old.close()
        self.eligibility = self._eligibility_for(backend)
        self.serving = not self.eligibility
        if isinstance(backend, OpenAICompatBackend) and backend.models:
            self._learn_bandwidth(backend.models[0], backend.tokens_per_s)

    def _learn_bandwidth(self, model_id: str, tps: float) -> None:
        """Back-solve memory bandwidth from a real measured decode speed (replaces the table value)."""
        m = catalog.BY_ID.get(model_id)
        if not m or tps <= 0 or not self.hw or not self.hw.is_gpu:
            return
        gbs = round(tps * m.size_gb * (m.active_b / m.params_b) / catalog.DECODE_EFFICIENCY, 1)
        self.hw.bandwidth_gbs, self.hw.bandwidth_source = gbs, "measured"
        self.measured_bandwidth = {"device": self.hw.device_name, "gbs": gbs, "model": model_id, "tps": round(tps, 1)}
        self.save_settings()

    async def serve_model(self, model_id: str) -> None:
        """Download (if needed) and serve a catalog model. Runs until ready or failed."""
        if model_id not in catalog.BY_ID:
            raise ValueError(f"unknown model {model_id!r}")
        await self.set_backend(None)
        self.serve_model_id = model_id
        self.save_settings()
        try:
            await self.models.start(model_id)
            await self.models.wait_ready()
            backend = OpenAICompatBackend(self.models.base_url, [model_id])
            await backend.start()
            await self.set_backend(backend)
        except Exception as exc:  # surfaced to the UI via models.snapshot()
            self.models.status, self.models.error = "error", str(exc)
            await self.models.stop()
            self.models.status, self.models.error = "error", str(exc)
            log.warning("serving %s failed: %s", model_id, exc)

    async def stop_serving(self) -> None:
        await self.set_backend(None)
        await self.models.stop()
        self.serve_model_id = None
        self.save_settings()

    # ------------------------------------------------------------------ lifecycle
    def _make_backend(self) -> Backend | None:
        b = self.cfg.backend
        if b.kind == "mock":
            return MockBackend(b.models or ["mock-7b"], b.mock_tokens_per_s)
        if b.kind == "openai":
            return OpenAICompatBackend(b.base_url, b.models, b.api_key_env)
        return None

    async def _probe(self) -> HardwareProfile:
        if self.cfg.simulated_hardware is not None:
            return HardwareProfile(**{**self.cfg.simulated_hardware, "simulated": True, "source": "simulated"})
        if not self.cfg.probe_hardware:
            return HardwareProfile("cpu", "unprobed", 0.0)
        hw = await asyncio.to_thread(probe, self.cfg.backend.server_cmd)
        mb = self.measured_bandwidth
        if mb and mb.get("device") == hw.device_name:  # a speed we measured on this exact device earlier
            hw.bandwidth_gbs, hw.bandwidth_source = float(mb["gbs"]), "measured"
        return hw

    async def start(self) -> None:
        from .api import ControlAPI  # local import: api depends on node

        self.hw = await self._probe()
        if self.backend is not None:
            await self.backend.start()
        self.eligibility = self._eligibility_for(self.backend)
        self.serving = not self.eligibility
        if self.cfg.backend.kind == "managed":
            self.models.reap_stale()
        if self.cfg.backend.kind == "managed" and self.serve_model_id and self.cfg.serve:
            self._tasks.append(asyncio.create_task(self.serve_model(self.serve_model_id)))  # resume
        if self.cfg.mesh.auto_join_global and self.trust.mesh_id is None:
            await self.join_global()
        if self.http:
            await self.http.start()
            self.mesh_port = await self._listen(self._mesh_app(), self.cfg.mesh.bind_host, self.cfg.mesh.port)
        await self.connect_relays()
        self.api = ControlAPI(self)
        self.control_port = await self._listen(self.api.app(), "127.0.0.1", self.cfg.control_port)
        await self.swarm_host.start()
        self._tasks.append(asyncio.create_task(self._gossip_loop()))
        log.info("node %s mesh=%s relays=%s control=127.0.0.1:%s serving=%s member=%s", self.id[:8],
                 f"{self.cfg.mesh.bind_host}:{self.mesh_port}" if self.http else "none (outbound only)",
                 len(self.relay.home) if self.relay else 0, self.control_port, self.serving, self.trust.is_member)

    async def _listen(self, app: web.Application, host: str, port: int) -> int:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        self._runners.append(runner)
        return runner.addresses[-1][1]

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.swarm_host.stop()
        if self.api:
            await self.api.close()
        for r in self._runners:
            await r.cleanup()
        if self.relay:
            await self.relay.stop()
        if self.http:
            await self.http.stop()
        if self.backend:
            await self.backend.close()
        await self.models.stop()
        self.ledger.flush()

    # ------------------------------------------------------------------ adverts & what others see
    @property
    def advertise_addr(self) -> str:
        return self.cfg.mesh.advertise_addr or f"{self.cfg.mesh.bind_host}:{self.mesh_port}"

    def location_cell(self) -> str | None:
        loc = self.cfg.location
        return public_cell(loc.share, loc.lat, loc.lon, loc.precision)

    def public_hw(self) -> dict | None:
        """Hardware as other members see it: only a coarse tier unless the owner opts in."""
        if self.hw is None:
            return None
        if self.cfg.visibility.device:
            d = self.hw.to_dict()
            return {k: d[k] for k in ("accelerator", "device_name", "accel_memory_gb", "bandwidth_gbs", "tier", "simulated")}
        return {"tier": self.hw.tier, "simulated": self.hw.simulated}

    def advert(self) -> dict:
        """This node's signed advert. Re-signed only when something routing-relevant changes or it
        nears expiry, so a quiet mesh carries almost no gossip churn."""
        body = self._advert_body()
        material = json.dumps(body, sort_keys=True)
        now = time.monotonic()
        cached = self._advert_cache
        if cached and cached[0] == material and now - cached[1] < self.cfg.mesh.advert_ttl_s / 3:
            return cached[2]
        self._seq = max(self._seq + 1, int(time.time() * 1000))
        env = self.identity.seal("advert", body, seq=self._seq)
        self._advert_cache = (material, now, env)
        return env

    def _advert_body(self) -> dict:
        """Everything here is visible to every member. Always present: node id + keys (in the
        envelope), membership proof, protocol version and what's needed to route work to you.
        Location, device model and activity are present only if the owner opted in."""
        g = self.governor
        body = {
            "version": __version__,
            "kx": self.kx.public_b64,
            # A private model (sharing off) is invisible to the mesh: not even its name is advertised.
            "role": "server" if self.sharing else "client",
            "hw": self.public_hw(),
            "models": list(self.backend.models) if self.sharing else [],
            "tps": self.backend.tokens_per_s if self.sharing else 0.0,
            # Coarse buckets: routers only need "plenty / some / none", and exact values would
            # force a re-signed advert every round.
            "budget_s": (5.0 * int(g.available_seconds() // 5) if g.available_seconds() >= 5 else round(g.available_seconds()))
                        if self.sharing else 0.0,
            "max_share": g.limits.max_share if (self.sharing or self.swarm_host.hosting) else 0.0,
            "max_tokens_per_job": g.limits.max_tokens_per_job,
            "active_jobs": g.active_jobs,
            "max_concurrent": g.limits.max_concurrent,
            "paused": g.limits.paused,
            "location": self.location_cell(),
            "membership": self.trust.proof(),
        }
        if self.http:
            body["addr"] = self.advertise_addr
        if self.relay:
            body["relays"] = self.relay.home_urls()
        stages = self.swarm_host.advert_entries()
        if stages:
            body["stages"] = stages
        if self.cfg.visibility.activity:
            body["served"] = self.counters["served_mesh"] // 10 * 10
        return body

    @property
    def sharing(self) -> bool:
        """Is this node's active model shared with the mesh? (Off = a private model, only for you.)"""
        return self.serving and not self.governor.limits.paused

    def contact_card(self) -> dict:
        """Answer to a cleartext hello: just the signed binding node id -> encryption key."""
        return self.identity.seal("contact", {"kx": self.kx.public_b64, "version": __version__}, seq=int(time.time() * 1000))

    def _dial_ok(self, addr: str) -> bool:
        host, _, port = addr.rpartition(":")
        if not port.isdigit() or not 0 < int(port) < 65536 or (self.http and addr == self.advertise_addr):
            return False
        return not _bad_dial_target(host, self.loopback_mesh)

    # ------------------------------------------------------------------ sealed RPC (client side)
    def _transport_for(self, hint: dict):
        if self.http and self.http.can_reach(hint):
            return self.http
        if self.relay and self.relay.can_reach(hint):
            return self.relay
        return None

    def _kx_for(self, node_id: str) -> str | None:
        rec = self.membership.peers.get(node_id)
        kx = rec.body.get("kx") if rec else None
        return kx if isinstance(kx, str) else self._contacts.get(node_id)

    async def _fetch_contact(self, node_id: str, hint: dict, transport) -> str:
        card = await transport.hello(node_id, hint)
        try:
            verify_relayed(card, expected_type="contact", max_age_s=300)
        except (EnvelopeError, TypeError) as exc:
            raise RouteError("peer sent an invalid contact card") from exc
        kx = card["body"].get("kx")
        if card.get("from") != node_id or not isinstance(kx, str):
            raise RouteError("contact card belongs to another node")
        if len(self._contacts) > 4096:
            self._contacts.clear()
        self._contacts[node_id] = kx
        return kx

    async def rpc(self, node_id: str, hint: dict, method: str, body: dict, timeout: float = 90) -> AsyncIterator[dict]:
        """Call ``method`` on a peer: sign, seal end-to-end, send, and decrypt the streamed reply."""
        transport = self._transport_for(hint)
        if transport is None:
            raise RouteError("no way to reach that peer from here")
        try:
            kx = self._kx_for(node_id) or await self._fetch_contact(node_id, hint, transport)
            env = self.identity.seal(method, body, to=node_id)
            blob, rk = seal_request(kx, node_id, method, env)
            opener = StreamOpener(rk)
            async for chunk in transport.call(node_id, hint, blob, timeout):
                item = opener.open(chunk)
                if item.get("_end"):
                    return
                if "_error" in item:
                    raise RemoteError(int(item.get("_status", 500)), str(item["_error"])[:300])
                yield item
        except (PeerUnreachable, SealError) as exc:
            raise RouteError(str(exc) or type(exc).__name__) from exc
        raise RouteError("stream ended without completion")

    async def rpc_one(self, node_id: str, hint: dict, method: str, body: dict, timeout: float = 30) -> dict:
        async for item in self.rpc(node_id, hint, method, body, timeout):
            return item
        raise RouteError("empty reply")

    def transport_label(self, hint: dict) -> dict:
        t = self._transport_for(hint)
        if t is None:
            return {"transport": "none"}
        if t is self.relay:
            relays = [r for r in self.relay.connected_relays() if r["url"] in (hint.get("relays") or [])]
            return {"transport": "relay", "relay": (relays[0]["relay_id"] if relays else "visited"), "e2e": True}
        return {"transport": "direct", "e2e": True}

    # ------------------------------------------------------------------ sealed RPC (server side)
    def _verify_member_request(self, env, msg_type: str) -> str:
        """Verify a direct envelope addressed to this node. Returns the sender id."""
        if not self.trust.is_member:
            raise EnvelopeError("this node has not joined a mesh")
        verify_direct(env, expected_type=msg_type, me=self.id, replay=self.replay)
        return env["from"]

    async def serve_rpc(self, blob: bytes, via: str) -> AsyncIterator[bytes]:
        """Decrypt one request, run its handler and yield the sealed reply stream."""
        method, env, rk = open_request(self.kx, self.id, blob)
        sealer = StreamSealer(rk)
        handlers = {"gossip": self._rpc_gossip, "infer": self._rpc_infer,
                    "stage.forward": self.swarm_host.rpc_forward, "stage.close": self.swarm_host.rpc_close,
                    "stage.result": self.swarm_client.rpc_result}
        try:
            handler = handlers.get(method)
            if handler is None:
                raise _Fail(404, "unknown method")
            sender = self._verify_member_request(env, method)
            body = env["body"]
            if method != "gossip" and sender not in self.membership.peers:
                raise _Fail(403, "unknown peer; join the mesh via gossip first")
            async for item in handler(sender, body):
                yield sealer.seal(item)
        except _Fail as f:
            yield sealer.seal({"_error": str(f), "_status": f.status})
        except EnvelopeError as exc:
            yield sealer.seal({"_error": str(exc), "_status": 403})
        except (KeyError, TypeError, ValueError) as exc:
            yield sealer.seal({"_error": f"bad request: {exc}"[:200], "_status": 400})
        yield sealer.seal({"_end": True})

    # ------------------------------------------------------------------ gossip
    async def _gossip_loop(self) -> None:
        while True:
            try:
                await self.gossip_once()
            except Exception:  # never let one bad round kill membership
                log.exception("gossip round failed")
            await asyncio.sleep(self.cfg.mesh.gossip_interval_s * random.uniform(0.8, 1.2))

    async def gossip_once(self) -> None:
        if not self.trust.is_member:
            return
        self.membership.prune()
        alive = self.membership.alive()
        targets: dict[str, dict] = {p.node_id: p.body for p in random.sample(alive, min(len(alive), self.cfg.mesh.gossip_fanout))}
        if len(alive) < self.cfg.mesh.gossip_fanout:
            for entry in self.bootstrap:
                try:
                    node_id, addr = parse_peer(entry)
                except ValueError:
                    continue
                if node_id != self.id:
                    targets.setdefault(node_id, {"addr": addr})
            if self.relay:  # relays double as rendezvous points for new nodes
                for node_id, hint in (await self.relay.rendezvous(8))[:8]:
                    targets.setdefault(node_id, hint)
        adverts = self.membership.adverts_for_gossip(self.advert())
        await asyncio.gather(*(self._gossip_with(nid, hint, adverts) for nid, hint in targets.items()
                               if self._transport_for(hint) is not None))

    def _absorb(self, data) -> None:
        adverts = data.get("adverts", []) if isinstance(data, dict) else []
        if isinstance(adverts, list):
            for env in adverts[:256]:
                if isinstance(env, dict):
                    self.membership.ingest(env)
        if isinstance(data, dict) and self.trust.accept_revocation(data.get("revocation")):
            self.membership.apply_revocations()
            self._save_membership()

    async def _gossip_with(self, node_id: str, hint: dict, adverts: list[dict]) -> None:
        t0 = time.monotonic()
        try:
            data = await self.rpc_one(node_id, hint, "gossip", {"adverts": adverts, "revocation": self.trust.revocation}, 20)
        except RouteError:
            return
        rtt = (time.monotonic() - t0) * 1000
        self._absorb(data)
        rec = self.membership.peers.get(node_id)
        if rec is not None:
            rec.rtt_ms = rtt if rec.rtt_ms == 0 else 0.7 * rec.rtt_ms + 0.3 * rtt

    async def _rpc_gossip(self, sender: str, body: dict) -> AsyncIterator[dict]:
        own = body["adverts"][0]
        if not isinstance(own, dict) or own.get("from") != sender:
            raise _Fail(400, "gossip must lead with the sender's own advert")
        window = self._gossip_seen.setdefault(sender, [])
        now = time.monotonic()
        window[:] = [t for t in window if now - t < 60][-120:]
        if len(window) >= 60:  # a member gossiping >1/s is misbehaving; don't let it burn our CPU
            raise _Fail(429, "slow down")
        window.append(now)
        if len(self._gossip_seen) > 4 * self.membership.max_peers:
            self._gossip_seen = {k: v for k, v in self._gossip_seen.items() if v and now - v[-1] < 60}
        self._absorb(body)
        if sender not in self.membership.peers:
            raise _Fail(403, "not a member of this mesh")  # non-members learn nothing
        yield {"adverts": self.membership.adverts_for_gossip(self.advert()), "revocation": self.trust.revocation}

    # ------------------------------------------------------------------ serving whole-model requests
    def _mesh_app(self) -> web.Application:
        app = web.Application(client_max_size=16 << 20)
        self.http.routes(app)
        return app

    async def _rpc_infer(self, sender: str, body: dict) -> AsyncIterator[dict]:
        if sender not in {p.node_id for p in self.membership.alive()}:
            raise _Fail(403, "unknown peer; join the mesh via gossip first")
        if not self.serving:
            raise _Fail(503, "node is not serving")
        try:
            model = body["model"]
            messages = validate_messages(body["messages"])
            tools = validate_tools(body.get("tools"))
            max_tokens = int(body.get("max_tokens", 512))
            temperature = float(body.get("temperature", 0.7))
            request_id = str(body["request_id"])[:64]
            if max_tokens < 1 or not 0 <= temperature <= 2:
                raise ValueError("max_tokens >= 1 and 0 <= temperature <= 2 required")
        except (KeyError, TypeError, ValueError) as exc:
            raise _Fail(400, f"invalid request: {exc}") from exc
        if model not in self.backend.models:
            raise _Fail(404, f"model {model!r} not served here")
        try:
            grant = self.governor.admit(request_id + sender, sender, max_tokens, self.backend.tokens_per_s)
        except Rejected as exc:
            raise _Fail(429, str(exc)) from exc
        parts: list[str] = []
        thoughts: list[str] = []
        tool_calls: list | None = None
        finish = "stop"
        t0 = time.monotonic()
        first = None
        try:
            try:
                async with asyncio.timeout(max(0.05, grant.deadline - time.monotonic())):
                    async for ev in self.backend.generate(model, messages, grant.max_tokens, temperature, tools):
                        first = first or time.monotonic()
                        if "tool_calls" in ev:
                            tool_calls, finish = ev["tool_calls"], "tool_calls"
                            yield {"tool_calls": tool_calls}
                            continue
                        if "reasoning" in ev:
                            thoughts.append(ev["reasoning"])
                            yield {"reasoning": ev["reasoning"]}
                        else:
                            parts.append(ev["delta"])
                            yield {"delta": ev["delta"]}
                        if len(parts) + len(thoughts) >= grant.max_tokens:
                            finish = "length"
                            break
            except TimeoutError:
                finish = "budget"
            except BackendError:
                finish = "error"
            receipt = self.identity.seal("receipt", {
                "request_id": request_id, "digest": output_digest("".join(parts), tool_calls, "".join(thoughts)),
                "tokens": len(parts) + len(thoughts), "finish": finish,
            }, to=sender)
            yield {"done": True, "finish_reason": finish, "tokens": len(parts),
                   "clamped_to": grant.max_tokens if grant.max_tokens < max_tokens else None, "receipt": receipt,
                   "server_timing": {"first_token_ms": round(((first or time.monotonic()) - t0) * 1000),
                                     "total_ms": round((time.monotonic() - t0) * 1000)}}
        finally:
            grant.finish()
            self.counters["served_mesh"] += 1
            self.counters["tokens_served_mesh"] += len(parts) + len(thoughts)
            self.ledger.record("given", len(parts) + len(thoughts), sender)

    # ------------------------------------------------------------------ routing (client side)
    def candidates(self, model: str, max_tokens: int, prefer_local: bool, exclude_self: bool) -> list[PeerRecord | None]:
        """Ordered places to run a whole-model request; ``None`` means this node."""
        scored: list[tuple[float, PeerRecord | None]] = []
        local_ok = self.serving and not exclude_self and model in self.backend.models
        if local_ok:
            scored.append((max_tokens / max(self.backend.tokens_per_s, 0.1), None))
        for p in self.membership.alive():
            b = p.body
            if b.get("role") != "server" or b.get("paused") or model not in b.get("models", []):
                continue
            if b.get("active_jobs", 0) >= b.get("max_concurrent", 1) or b.get("budget_s", 0) < 1.0:
                continue
            if p.ok + p.failed >= 5 and p.reliability < 0.4:
                continue
            if self._transport_for(b) is None:
                continue
            tps = p.observed_tps or 0.7 * float(b.get("tps", 0) or 0)  # distrust claims until measured
            if tps <= 0:
                continue
            # Typical replies are far shorter than max_tokens; estimate with a realistic length.
            tokens = min(max_tokens, b.get("max_tokens_per_job", max_tokens), 256)
            est = p.rtt_ms / 1000 + tokens / tps
            claimed = float(b.get("tps", 0) or 0)
            # A node whose budget can't cover the whole job would truncate it: last resort only.
            short_budget = claimed > 0 and b.get("budget_s", 0) < 1.0 + tokens / claimed
            scored.append((est / p.reliability * (100 if short_budget else 1), p))
        scored.sort(key=lambda s: s[0])
        if scored:  # spread load among options within 25% of the best estimate
            best = scored[0][0]
            near = [s for s in scored if s[0] <= best * 1.25]
            random.shuffle(near)
            scored = near + scored[len(near):]
        order = [p for _, p in scored]
        if local_ok and prefer_local:  # keeps prompts on this machine whenever it can serve
            order.remove(None)
            order.insert(0, None)
        return order[:MAX_ATTEMPTS]

    def swarm_overview(self) -> dict[str, dict]:
        from .swarm import planner
        return planner.overview(self)

    def model_catalog(self) -> list[dict]:
        """Every model on the mesh — whole-model replicas and swarms — for pickers and /v1/models."""
        rank = {"T3": 3, "T2": 2, "T1": 1}
        out: dict[str, dict] = {}
        rows = []
        if self.serving:
            rows.append((self.hw.tier if self.hw else "", self.backend.models, self.backend.tokens_per_s, True, True))
        for p in self.membership.alive():
            b = p.body
            if b.get("role") == "server":
                rows.append(((b.get("hw") or {}).get("tier", ""), b.get("models", []), p.observed_tps or b.get("tps", 0),
                             not b.get("paused") and b.get("budget_s", 0) >= 1, False))

        def entry(m):
            return out.setdefault(m, {"id": m, "nodes": 0, "available": 0, "tier": "", "tps": 0.0, "local": False, "swarm": None})
        for tier, models, tps, available, local in rows:
            for m in models:
                e = entry(m)
                e["nodes"] += 1
                e["available"] += 1 if available else 0
                e["tps"] = max(e["tps"], float(tps or 0))
                e["local"] = e["local"] or local
                if rank.get(tier, 0) > rank.get(e["tier"], 0):
                    e["tier"] = tier
        for mid, sw in self.swarm_overview().items():
            entry(mid)["swarm"] = sw
        for e in out.values():
            m = catalog.BY_ID.get(e["id"])
            e.update(name=m.name if m else e["id"], capability=m.capability if m else 0,
                     memory_gb=m.memory_gb if m else None)
        return sorted(out.values(), key=lambda e: (-e["capability"], -rank.get(e["tier"], 0), -e["available"], e["id"]))

    def best_model(self) -> str | None:
        """'Auto': the most capable model one node can serve right now; else a complete swarm."""
        entries = self.model_catalog()
        for e in entries:
            if e["available"]:
                return e["id"]
        for e in entries:
            if e["swarm"] and e["swarm"]["complete"]:
                return e["id"] + SWARM_SUFFIX
        return None

    async def route(self, model: str | None, messages: list[dict], max_tokens: int, temperature: float,
                    prefer_local: bool = True, exclude_self: bool = False, tools: list | None = None) -> AsyncIterator[dict]:
        """Yield {"delta"} / {"reasoning"} / {"tool_calls"} / {"progress"} events, then one {"done": True, ...}."""
        messages = validate_messages(messages)
        tools = validate_tools(tools)
        base, mode = split_model(model or self.best_model())
        if base is None:
            raise RouteError("no model is available on the mesh right now")
        errors: list[dict] = []
        if mode != "swarm":
            order = self.candidates(base, max_tokens, prefer_local, exclude_self)
            for attempt, peer in enumerate(order, 1):
                emitted = False
                try:
                    gen = (self._run_local(base, messages, max_tokens, temperature, tools) if peer is None
                           else self._run_remote(peer, base, messages, max_tokens, temperature, tools))
                    async for event in gen:
                        if "done" in event:
                            event.update(attempts=attempt, errors=errors, model=base, mode="local" if peer is None else "direct")
                        elif "progress" not in event:
                            emitted = True
                        yield event
                    self.counters["routed_ok"] += 1
                    return
                except (RouteError, BackendError) as exc:
                    errors.append({"node": "self" if peer is None else peer.node_id, "error": str(exc) or type(exc).__name__})
                    if emitted:  # can't transparently retry after streaming partial output
                        self.counters["routed_failed"] += 1
                        yield {"done": True, "finish_reason": "error", "served_by": errors[-1]["node"],
                               "attempts": attempt, "errors": errors, "model": base, "mode": "direct"}
                        return
            sw = self.swarm_overview().get(base)
            if not (sw and sw["complete"]):  # nothing left to try
                self.counters["routed_failed"] += 1
                if errors:
                    raise RouteError(f"all {len(order)} candidate nodes failed: {errors}")
                raise RouteError(f"no node on the mesh can serve {base} right now")
        async for event in self.swarm_client.generate(base, messages, max_tokens, temperature, tools, exclude_self):
            if "done" in event:
                event.setdefault("errors", errors)
            yield event
        self.counters["routed_ok"] += 1

    def hop_view(self, node_id: str, body: dict | None, peer: PeerRecord | None) -> dict:
        """One node on a request's path, as the requester may show it (respects each node's privacy)."""
        body = body or {}
        hw = body.get("hw") or {}
        cell = body.get("location")
        lat = lon = None
        if cell:
            lat, lon, _, _ = decode_center(cell)
        return {"node": node_id, "self": node_id == self.id, "device": hw.get("device_name"), "tier": hw.get("tier"),
                "simulated": hw.get("simulated", False), "location": cell, "lat": lat, "lon": lon,
                "rtt_ms": round(peer.rtt_ms, 1) if peer and peer.rtt_ms else None}

    def _peer_meta(self, peer: PeerRecord | None) -> dict:
        body = self.advert()["body"] if peer is None else peer.body
        hw = body.get("hw") or {}
        hop = self.hop_view(self.id if peer is None else peer.node_id, body, peer)
        path = {"mode": "local" if peer is None else "direct", "hops": [hop],
                **({"transport": "local", "e2e": True} if peer is None else self.transport_label(body))}
        return {"served_by": self.id if peer is None else peer.node_id, "local": peer is None,
                "tier": hw.get("tier"), "device": hw.get("device_name"), "simulated": hw.get("simulated", False),
                "location": body.get("location"), "path": path}

    async def _run_local(self, model, messages, max_tokens, temperature, tools) -> AsyncIterator[dict]:
        n, t0, first, finish = 0, time.monotonic(), None, "stop"
        async for ev in self.backend.generate(model, messages, max_tokens, temperature, tools):
            if "tool_calls" in ev:
                finish = "tool_calls"
            else:  # answer text and reasoning both cost GPU time
                n += 1
                first = first or time.monotonic()
            yield ev
        self.counters["served_local"] += 1
        tps = (n - 1) / max(1e-3, time.monotonic() - first) if first and n > 1 else None
        yield {"done": True, "finish_reason": "length" if n >= max_tokens else finish, "tokens": n,
               "receipt_verified": True, "tokens_per_s": round(tps, 1) if tps else None,
               "latency_ms": round(((first or time.monotonic()) - t0) * 1000), **self._peer_meta(None)}

    async def _run_remote(self, peer: PeerRecord, model, messages, max_tokens, temperature, tools) -> AsyncIterator[dict]:
        request_id = secrets.token_hex(8)
        body = {"request_id": request_id, "model": model, "messages": messages,
                "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
        t0, first, parts, thoughts, tool_calls = time.monotonic(), None, [], [], None
        try:
            async for event in self.rpc(peer.node_id, peer.body, "infer", body, timeout=60):
                if "delta" in event:
                    first = first or time.monotonic()
                    parts.append(str(event["delta"]))
                    yield {"delta": parts[-1]}
                elif "reasoning" in event:
                    first = first or time.monotonic()
                    thoughts.append(str(event["reasoning"]))
                    yield {"reasoning": thoughts[-1]}
                elif "tool_calls" in event:
                    tool_calls = event["tool_calls"]
                    yield {"tool_calls": tool_calls}
                elif event.get("done"):
                    if not self._check_receipt(event.get("receipt"), peer.node_id, request_id,
                                               output_digest("".join(parts), tool_calls, "".join(thoughts))):
                        peer.failed += 1
                        raise RouteError("invalid receipt from peer")
                    peer.ok += 1
                    tps = None
                    produced = len(parts) + len(thoughts)
                    if first and produced > 4:
                        tps = (produced - 1) / max(1e-3, time.monotonic() - first)
                        peer.observed_tps = tps if not peer.observed_tps else 0.7 * peer.observed_tps + 0.3 * tps
                    self.counters["tokens_from_mesh"] += produced
                    self.ledger.record("received", produced)
                    meta = self._peer_meta(peer)
                    meta["path"]["server_timing"] = event.get("server_timing")
                    yield {"done": True, "finish_reason": event.get("finish_reason"), "tokens": produced,
                           "clamped_to": event.get("clamped_to"), "receipt_verified": True,
                           "tokens_per_s": round(tps, 1) if tps else None,
                           "latency_ms": round(((first or time.monotonic()) - t0) * 1000), **meta}
                    return
        except RemoteError as exc:
            if exc.status != 429:  # being busy is not unreliability
                peer.failed += 1
            raise
        except RouteError:
            peer.failed += 1
            raise
        peer.failed += 1
        raise RouteError("stream ended without completion")

    def _check_receipt(self, receipt, peer_id: str, request_id: str, digest: str) -> bool:
        try:
            verify_direct(receipt, expected_type="receipt", me=self.id, replay=self.replay)
        except (EnvelopeError, TypeError, AttributeError):
            return False
        b = receipt["body"]
        return receipt["from"] == peer_id and b.get("request_id") == request_id and b.get("digest") == digest

    # ------------------------------------------------------------------ views (used by the API)
    def node_views(self) -> list[dict]:
        def view(node_id, body, peer):
            cell = body.get("location")
            lat = lon = None
            if cell:
                lat, lon, _, _ = decode_center(cell)
            hw = body.get("hw") or {}
            proof = body.get("membership") or {}
            certs = (proof.get("chain") or []) if proof.get("kind") == "private" else []
            return {
                "node_id": node_id, "self": peer is None, "role": body.get("role"), "tier": hw.get("tier"),
                "device": hw.get("device_name"), "accelerator": hw.get("accelerator"),
                "accel_memory_gb": hw.get("accel_memory_gb"), "simulated": hw.get("simulated", False),
                "models": body.get("models", []), "tps_claimed": body.get("tps"),
                "tps_observed": round(peer.observed_tps, 1) if peer and peer.observed_tps else None,
                "budget_s": body.get("budget_s"), "max_share": body.get("max_share"),
                "active_jobs": body.get("active_jobs"), "max_concurrent": body.get("max_concurrent"),
                "paused": body.get("paused"), "served": body.get("served"),
                "stages": body.get("stages") or [],
                "transport": "relay" if body.get("relays") else ("direct" if body.get("addr") else "none"),
                "location": cell, "lat": lat, "lon": lon,
                "invited_by": certs[-1]["body"]["iss"] if len(certs) > 1 else None,
                "founder": len(certs) == 1,
                "mesh_kind": proof.get("kind"),
                "rtt_ms": round(peer.rtt_ms, 1) if peer else 0.0,
                "reliability": round(peer.reliability, 2) if peer else 1.0,
                "last_seen_s": round(time.monotonic() - peer.received_at, 1) if peer else 0.0,
            }
        return [view(self.id, self.advert()["body"], None)] + [view(p.node_id, p.body, p) for p in self.membership.alive()]

    def stats(self) -> dict:
        nodes = self.node_views()
        servers = [n for n in nodes if n["role"] == "server" or n["stages"]]
        tiers: dict[str, int] = {}
        for n in servers:
            tiers[n["tier"] or "?"] = tiers.get(n["tier"] or "?", 0) + 1
        # Members who keep their hardware private count at their tier's floor.
        mem = sum(n["accel_memory_gb"] if n["accel_memory_gb"] is not None else TIER_FLOOR_GB.get(n["tier"], 0) for n in servers)
        return {
            "active_nodes": len(nodes),
            "serving_nodes": len(servers),
            "available_now": sum(1 for n in servers if not n["paused"] and (n["budget_s"] or 0) >= 1
                                 and (n["active_jobs"] or 0) < (n["max_concurrent"] or 1)),
            "client_nodes": len(nodes) - len(servers),
            "accel_memory_gb": round(mem, 1),
            "hardware_private": sum(1 for n in servers if n["accel_memory_gb"] is None),
            "sustained_tokens_per_s": round(sum((n["tps_claimed"] or 0) * (n["max_share"] or 0) for n in servers), 1),
            "models": {m["id"]: m["nodes"] for m in self.model_catalog() if m["nodes"]},
            "swarms": {k: v for k, v in self.swarm_overview().items()},
            "tiers": tiers,
            "regions": len({n["location"][:2] for n in nodes if n["location"]}),
            "located_nodes": sum(1 for n in nodes if n["location"]),
            "simulated_nodes": sum(1 for n in nodes if n["simulated"]),
            "relay_nodes": sum(1 for n in nodes if n["transport"] == "relay"),
            "adverts_rejected": self.membership.rejected,
            "counters": self.counters,
        }

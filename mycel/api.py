"""Loopback-only control plane: the app UI, the local agent endpoint and settings APIs.

Bound to 127.0.0.1 with no override (see netguard). Agents on this machine use the
OpenAI-compatible ``/v1`` routes; the app uses ``/api``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import __version__
from . import models as catalog
from .governor import Limits
from .location import PRECISIONS, public_cell
from .netguard import control_plane_guard
from .plugins import PluginError, PluginManager
from .store import ConversationStore
from .trust import TrustError, issue_cert, join_code, membership_code, parse_join_code

if TYPE_CHECKING:
    from .node import Node

STATIC = Path(__file__).parent / "static"
LIMIT_KEYS = {"max_share", "max_concurrent", "max_tokens_per_job", "daily_seconds", "per_peer_per_min", "paused"}


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "expected a JSON object"}), content_type="application/json")
    return data


class ControlAPI:
    def __init__(self, node: "Node"):
        self.node = node
        self.plugins = PluginManager(node.data_dir, node.stats)
        self.conversations = ConversationStore(node.data_dir / "conversations")

    async def close(self) -> None:
        await self.plugins.close()

    def app(self) -> web.Application:
        app = web.Application(middlewares=[control_plane_guard], client_max_size=8 << 20)
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/static", STATIC)
        r.add_get("/api/status", self.status)
        r.add_get("/api/nodes", self.nodes)
        r.add_get("/api/stats", self.stats)
        r.add_get("/api/models", self.models)
        r.add_get("/api/impact", self.impact)
        r.add_get("/api/library", self.library)
        r.add_post("/api/library/serve", self.library_serve)
        r.add_post("/api/library/stop", self.library_stop)
        r.add_post("/api/limits", self.limits)
        r.add_post("/api/location", self.location)
        r.add_get("/api/visibility", self.visibility)
        r.add_post("/api/visibility", self.visibility_set)
        r.add_get("/api/swarm", self.swarm)
        r.add_post("/api/swarm/host", self.swarm_host)
        r.add_get("/api/network", self.network)
        r.add_post("/api/network/relays", self.network_relay_add)
        r.add_post("/api/network/relays/remove", self.network_relay_remove)
        r.add_post("/api/network/bundled", self.network_bundled)
        r.add_get("/api/membership", self.membership)
        r.add_post("/api/membership/join-global", self.membership_join_global)
        r.add_post("/api/membership/create", self.membership_create)
        r.add_post("/api/membership/block", self.membership_block)
        r.add_post("/api/membership/join", self.membership_join)
        r.add_post("/api/membership/invite", self.membership_invite)
        r.add_post("/api/membership/revoke", self.membership_revoke)
        r.add_post("/api/membership/leave", self.membership_leave)
        r.add_get("/api/plugins", self.plugin_catalog)
        r.add_post("/api/plugins/preview", self.plugin_preview)
        r.add_post("/api/plugins/install", self.plugin_install)
        r.add_post("/api/plugins/custom", self.plugin_custom)
        r.add_post("/api/plugins/uninstall", self.plugin_uninstall)
        r.add_post("/api/plugins/update", self.plugin_update)
        r.add_get("/api/plugins/tools", self.plugin_tools)
        r.add_post("/api/plugins/call", self.plugin_call)
        r.add_get("/api/conversations", self.conv_list)
        r.add_post("/api/conversations", self.conv_create)
        r.add_get("/api/conversations/{cid}", self.conv_get)
        r.add_put("/api/conversations/{cid}", self.conv_put)
        r.add_delete("/api/conversations/{cid}", self.conv_delete)
        r.add_post("/api/chat", self.chat)
        r.add_get("/v1/models", self.v1_models)
        r.add_post("/v1/chat/completions", self.v1_chat)
        return app

    # ------------------------------------------------------------------ app shell & status
    async def index(self, _r):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def status(self, _r):
        n = self.node
        loc = n.cfg.location
        return web.json_response({
            "node_id": n.id, "version": __version__, "serving": n.serving,
            "eligibility_problems": n.eligibility, "hardware": n.hw.to_dict() if n.hw else None,
            "backend": {"models": n.backend.models if n.backend and n.serving else [],
                        "tokens_per_s": n.backend.tokens_per_s if n.backend and n.serving else 0},
            "governor": n.governor.snapshot(), "member": n.trust.is_member,
            "serve_model": n.serve_model_id, "model_server": n.models.snapshot(),
            "location": {"share": loc.share, "precision": loc.precision, "lat": loc.lat, "lon": loc.lon,
                         "public_cell": n.location_cell(), "precisions": list(PRECISIONS)},
            "counters": n.counters, "uptime_s": round(time.time() - n.started_at),
            "control_url": f"http://127.0.0.1:{n.control_port}",
            "swarm": n.swarm_host.snapshot(), "visibility": n.cfg.visibility.__dict__,
            "sharing": n.sharing,
            "network": n.network_view(),
        })

    async def nodes(self, _r):
        return web.json_response({"nodes": self.node.node_views()})

    async def stats(self, _r):
        return web.json_response(self.node.stats())

    async def models(self, _r):
        return web.json_response({"models": self.node.model_catalog(), "auto": self.node.best_model()})

    async def impact(self, _r):
        return web.json_response(self.node.ledger.summary())

    async def library(self, _r):
        n = self.node
        hw = n.hw
        accel, bw = (hw.accel_memory_gb, hw.bandwidth_gbs) if hw else (0.0, 0.0)
        mesh = {m["id"]: m for m in n.model_catalog()}
        rows = []
        for m in catalog.CATALOG:
            d = catalog.describe(m.id, accel if hw and hw.accelerator in ("cuda", "mps") else 0.0, bw)
            on_mesh = mesh.get(m.id, {})
            serving_here = n.serving and n.backend is not None and m.id in n.backend.models
            rows.append({**d, "downloaded": n.models.downloaded(m.id), "serving_here": serving_here,
                         "selected": n.serve_model_id == m.id,
                         "mesh_nodes": on_mesh.get("nodes", 0) - (1 if serving_here else 0),
                         "mesh_available": on_mesh.get("available", 0) - (1 if serving_here else 0),
                         "mesh_tps": on_mesh.get("tps")})
        return web.json_response({
            "models": rows, "recommended": catalog.recommend(accel, bw) if hw and hw.accelerator in ("cuda", "mps") else None,
            "server": n.models.snapshot(), "serve_model": n.serve_model_id, "serving": n.serving,
            "device": {"accel_memory_gb": accel, "bandwidth_gbs": bw, "name": hw.device_name if hw else None,
                       "can_serve_hardware": not [p for p in n.eligibility if "GPU" in p or "memory" in p or "TFLOPS" in p]},
        })

    async def library_serve(self, request):
        data = await _json(request)
        mid = str(data.get("id", ""))
        if mid not in catalog.BY_ID:
            return _err("unknown model")
        if self.node.cfg.backend.kind != "managed":
            return _err("this node uses a fixed backend from its config file")
        if not self.node.models.available:
            return _err("llama-server isn't installed. Install llama.cpp first (macOS: brew install llama.cpp).")
        if isinstance(data.get("share"), bool):   # run privately, or run and share
            self.node.governor.update(paused=not data["share"])
            self.node.save_settings()
        asyncio.create_task(self.node.serve_model(mid))
        await asyncio.sleep(0.05)
        return web.json_response({"ok": True, "server": self.node.models.snapshot()})

    async def library_stop(self, _r):
        await self.node.stop_serving()
        return web.json_response({"ok": True})

    async def limits(self, request):
        data = await _json(request)
        if set(data) - LIMIT_KEYS:
            return _err(f"allowed keys: {sorted(LIMIT_KEYS)}")
        try:
            limits: Limits = self.node.governor.update(**data)
        except (ValueError, TypeError) as exc:
            return _err(str(exc))
        self.node.save_settings()
        return web.json_response({"limits": limits.__dict__})

    async def location(self, request):
        data = await _json(request)
        loc = self.node.cfg.location
        try:
            share = bool(data.get("share", loc.share))
            precision = data.get("precision", loc.precision)
            lat, lon = data.get("lat", loc.lat), data.get("lon", loc.lon)
            lat = None if lat in (None, "") else float(lat)
            lon = None if lon in (None, "") else float(lon)
            if share and (lat is None or lon is None):
                raise ValueError("set a latitude and longitude to share a location")
            public_cell(share, lat, lon, precision)  # validates range & precision
        except (ValueError, TypeError) as exc:
            return _err(str(exc))
        loc.share, loc.precision, loc.lat, loc.lon = share, precision, lat, lon
        self.node.save_settings()
        return web.json_response({"public_cell": self.node.location_cell()})

    # ------------------------------------------------------------------ what others see
    def _visibility_view(self) -> dict:
        n = self.node
        body = dict(n.advert()["body"])
        proof = body.pop("membership", None) or {}
        body["membership"] = {"kind": proof.get("kind"), "…": "proof of membership (a signed ticket or invitation chain)"}
        return {
            "device": n.cfg.visibility.device, "activity": n.cfg.visibility.activity,
            "location": {"share": n.cfg.location.share, "public_cell": n.location_cell()},
            "always": {"node_id": n.id, "transport": "relay" if n.relay and not n.http else ("direct" if n.http else "none"),
                       "address_shared": "addr" in body},
            "advert": {"from": n.id, "body": body},
        }

    async def visibility(self, _r):
        return web.json_response(self._visibility_view())

    async def visibility_set(self, request):
        data = await _json(request)
        if set(data) - {"device", "activity"} or not all(isinstance(v, bool) for v in data.values()):
            return _err("allowed keys: device, activity (booleans). Location has its own setting.")
        for k, v in data.items():
            setattr(self.node.cfg.visibility, k, v)
        self.node.save_settings()
        return web.json_response(self._visibility_view())

    # ------------------------------------------------------------------ swarm hosting
    async def swarm(self, _r):
        n = self.node
        overview = n.swarm_overview()
        hw = n.hw
        rows = []
        for m in catalog.CATALOG:
            if not m.swarm or m.id.startswith("tiny-"):
                continue
            cap = n.swarm_host.max_layers(m.id)
            cap_free = n.swarm_host.max_layers(m.id, ignore_active=True)
            engine = n.swarm_host.engine_kind()
            rows.append({"id": m.id, "name": m.name, "capability": m.capability, "layers": m.layers,
                         "layer_gb": m.layer_gb_for(engine), "download_layer_gb": m.layer_gb, "size_gb": m.size_gb, "bookend_gb": m.bookend_gb,
                         "capacity_layers": cap, "slice_gb": round(cap * m.layer_gb_for(engine), 1),
                         "capacity_if_free": cap_free, "blocked_by_active": cap < 1 <= cap_free,
                         "fits_whole": bool(hw and hw.is_gpu and hw.accel_memory_gb * 0.8 >= m.memory_gb),
                         "problems": n.swarm_host.problems(m.id),
                         "est_layer_ms": m.est_layer_ms(hw.bandwidth_gbs if hw else 0.0),
                         "mesh": overview.get(m.id)})
        hostable = [r for r in rows if (not r["problems"] or r["blocked_by_active"]) and not r["fits_whole"]]
        suggested = max(hostable, key=lambda r: (r["capability"], r["layers"]))["id"] if hostable else None
        active = n.backend.models[0] if n.serving and n.backend and n.backend.models else None
        return web.json_response({"hosting": n.swarm_host.snapshot(), "models": rows, "suggested": suggested,
                                  "engine": n.swarm_host.engine_kind(),
                                  "active_model": active, "active_memory_gb": catalog.BY_ID[active].memory_gb if active in catalog.BY_ID else None})

    async def swarm_host(self, request):
        data = await _json(request)
        model = data.get("model")
        if model is not None and model not in catalog.BY_ID:
            return _err("unknown model")
        layers = data.get("layers")
        if layers is not None and (not isinstance(layers, int) or layers < 0):
            return _err("layers must be a non-negative integer")
        if model is not None and data.get("replace_active") is True and self.node.serving:
            await self.node.stop_serving()   # free the GPU memory the active model was using
        try:
            await self.node.swarm_host.set_model(model, layers=layers)
        except ValueError as exc:
            return _err(str(exc))
        return await self.swarm(request)

    # ------------------------------------------------------------------ membership
    async def membership(self, _r):
        n, t = self.node, self.node.trust
        leaf = t.chain[-1]["body"] if t.kind == "private" and t.chain else None
        return web.json_response({
            "member": t.is_member, "kind": t.kind, "mesh_id": t.mesh_id,
            "founder": t.is_founder, "can_invite": t.can_invite,
            "join_code": join_code(n.identity), "node_id": n.id, "has_ticket": t.ticket is not None,
            "expires": leaf["exp"] if leaf else None, "invited_by": leaf["iss"] if leaf and t.chain[1:] else None,
            "members_online": len(n.membership.alive()) + (1 if t.is_member else 0),
            "revoked": sorted(t.revoked), "blocked": sorted(t.blocked),
        })

    # ------------------------------------------------------------------ network (relays)
    async def network(self, _r):
        return web.json_response(self.node.network_view())

    async def network_relay_add(self, request):
        data = await _json(request)
        url = data.get("url")
        if not isinstance(url, str):
            return _err("url required")
        try:
            await self.node.add_relay(url)
        except ValueError as exc:
            return _err(str(exc))
        return web.json_response(self.node.network_view())

    async def network_relay_remove(self, request):
        data = await _json(request)
        try:
            await self.node.remove_relay(str(data.get("url", "")))
        except ValueError as exc:
            return _err(str(exc))
        return web.json_response(self.node.network_view())

    async def network_bundled(self, request):
        data = await _json(request)
        if not self.node.relay_book or not isinstance(data.get("on"), bool):
            return _err("internet mode and {on: bool} required")
        self.node.relay_book.set_use_bundled(data["on"])
        return web.json_response(self.node.network_view())

    async def membership_join_global(self, request):
        await self.node.join_global()
        await self.node.gossip_once()
        return await self.membership(request)

    async def membership_create(self, _r):
        self.node.create_private_mesh()
        return await self.membership(_r)

    async def membership_join(self, request):
        data = await _json(request)
        try:
            self.node.join_private(str(data.get("code", "")))
        except TrustError as exc:
            return _err(str(exc))
        await self.node.gossip_once()
        return await self.membership(request)

    async def membership_block(self, request):
        data = await _json(request)
        try:
            self.node.block(str(data.get("node_id", "")))
        except TrustError as exc:
            return _err(str(exc))
        return await self.membership(request)

    async def membership_invite(self, request):
        data = await _json(request)
        n = self.node
        if n.trust.kind != "private":
            return _err("Invitations are for private meshes. Anyone can join the global mesh — "
                        "they just install Mycel and tap Join.")
        try:
            node_id, pubkey = parse_join_code(str(data.get("join_code", "")))
            if node_id == n.id:
                raise TrustError("that's your own join code — send it to someone who's already a member")
            chain = issue_cert(n.identity, n.trust.chain, node_id, pubkey, bool(data.get("can_invite", False)))
        except TrustError as exc:
            return _err(str(exc))
        peers = [n.peer_entry()] + [f"{p.node_id}@{p.body['addr']}" for p in n.membership.alive()[:5]]
        return web.json_response({"membership_code": membership_code(chain, peers), "node_id": node_id})

    async def membership_revoke(self, request):
        data = await _json(request)
        try:
            self.node.revoke(str(data.get("node_id", "")))
        except TrustError as exc:
            return _err(str(exc), 403)
        return await self.membership(request)

    async def membership_leave(self, request):
        data = await _json(request)
        if data.get("confirm") is not True:
            return _err("confirmation required")
        self.node.leave_mesh()
        return await self.membership(request)

    # ------------------------------------------------------------------ plugins
    async def plugin_catalog(self, _r):
        return web.json_response({"plugins": self.plugins.catalog()})

    async def plugin_preview(self, request):
        data = await _json(request)
        try:
            return web.json_response({"command": self.plugins.resolved_command(str(data.get("id")), data.get("config") or {})})
        except PluginError as exc:
            return _err(str(exc))

    async def plugin_install(self, request):
        data = await _json(request)
        try:
            out = await self.plugins.install(str(data.get("id")), data.get("config") or {}, data.get("confirm_command"))
        except PluginError as exc:
            return _err(str(exc))
        return web.json_response(out)

    async def plugin_custom(self, request):
        data = await _json(request)
        cmd = data.get("command")
        try:
            if not isinstance(cmd, list):
                raise PluginError("command must be a list of arguments")
            pid = await self.plugins.add_custom(str(data.get("name", "")), cmd, data.get("confirm") is True)
        except PluginError as exc:
            return _err(str(exc))
        return web.json_response({"id": pid})

    async def plugin_uninstall(self, request):
        data = await _json(request)
        try:
            await self.plugins.uninstall(str(data.get("id")))
        except PluginError as exc:
            return _err(str(exc))
        return web.json_response({"ok": True})

    async def plugin_update(self, request):
        data = await _json(request)
        try:
            await self.plugins.update(str(data.get("id")), data.get("enabled"), data.get("permission"))
        except PluginError as exc:
            return _err(str(exc))
        return web.json_response({"ok": True})

    async def plugin_tools(self, _r):
        return web.json_response({"tools": await self.plugins.tools()})

    async def plugin_call(self, request):
        data = await _json(request)
        try:
            out = await self.plugins.call(str(data.get("name")), data.get("arguments", {}), data.get("approved") is True)
        except PluginError as exc:
            return _err(str(exc), 403 if "approval" in str(exc) else 400)
        return web.json_response(out)

    # ------------------------------------------------------------------ conversations
    async def conv_list(self, _r):
        return web.json_response({"conversations": self.conversations.list()})

    async def conv_create(self, request):
        data = await _json(request)
        return web.json_response(self.conversations.create(str(data.get("title", "New chat"))[:120], data.get("model")))

    async def conv_get(self, request):
        try:
            return web.json_response(self.conversations.get(request.match_info["cid"]))
        except KeyError:
            return _err("not found", 404)

    async def conv_put(self, request):
        data = await _json(request)
        try:
            return web.json_response(self.conversations.put(request.match_info["cid"], data))
        except KeyError:
            return _err("not found", 404)
        except ValueError as exc:
            return _err(str(exc))

    async def conv_delete(self, request):
        try:
            self.conversations.delete(request.match_info["cid"])
        except KeyError:
            return _err("not found", 404)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ chat
    def _route(self, data: dict):
        model = data.get("model")
        if model in (None, "", "auto", "mesh-auto"):
            model = None
        opts = data.get("mesh") or {}
        prefer_local = bool(opts.get("prefer_local", True))
        n = self.node
        if model is None and prefer_local and n.serving and n.backend and n.backend.models:
            model = n.backend.models[0]  # "Private first": Auto uses your own GPU's model when you serve one
        return self.node.route(model, data["messages"], int(data.get("max_tokens") or 2048),
                               float(data.get("temperature", 0.7)), prefer_local,
                               bool(opts.get("exclude_self", False)), data.get("tools"))

    async def chat(self, request):
        """App chat stream: NDJSON of route events (delta / tool_calls / done)."""
        from .node import RouteError

        data = await _json(request)
        try:
            events = self._route(data)
            first = await anext(events)
        except (KeyError, TypeError, ValueError) as exc:
            return _err(f"invalid request: {exc}")
        except RouteError as exc:
            return _err(str(exc), 503)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        try:
            await resp.write(json.dumps(first).encode() + b"\n")
            async for ev in events:
                await resp.write(json.dumps(ev).encode() + b"\n")
        except ConnectionResetError:
            return resp
        except RouteError as exc:
            await resp.write(json.dumps({"done": True, "finish_reason": "error", "error": str(exc)}).encode() + b"\n")
        await resp.write_eof()
        return resp

    # ------------------------------------------------------------------ OpenAI-compatible agent endpoint
    async def v1_models(self, _r):
        created = int(self.node.started_at)
        data = [{"id": "auto", "object": "model", "created": created, "owned_by": "mycel",
                 "description": "Best model available on the mesh right now"}]
        for m in self.node.model_catalog():
            if m["nodes"]:
                data.append({"id": m["id"], "object": "model", "created": created, "owned_by": "mycel",
                             "nodes": m["nodes"], "tier": m["tier"]})
            if m["swarm"] and m["swarm"]["complete"]:
                data.append({"id": m["id"] + "@swarm", "object": "model", "created": created, "owned_by": "mycel",
                             "description": f"Split across {m['swarm']['hops']} GPUs; your text stays on this device",
                             "swarm_hosts": m["swarm"]["hosts"], "est_tokens_per_s": m["swarm"]["est_tps"]})
        return web.json_response({"object": "list", "data": data})

    async def v1_chat(self, request):
        from .node import RouteError

        def oai_error(message, status, kind="invalid_request_error"):
            return web.json_response({"error": {"message": message, "type": kind}}, status=status)

        data = await _json(request)
        stream = bool(data.get("stream"))
        try:
            events = self._route(data)
            first = await anext(events)
        except (KeyError, TypeError, ValueError) as exc:
            return oai_error(f"invalid request: {exc}", 400)
        except RouteError as exc:
            return oai_error(str(exc), 503, "service_unavailable")
        cid, created = "chatcmpl-" + secrets.token_hex(12), int(time.time())
        model_name = data.get("model") or "auto"

        def mesh_meta(done):
            return {k: done.get(k) for k in ("model", "mode", "served_by", "local", "receipt_verified", "attempts",
                                             "tokens_per_s", "tier", "clamped_to", "path")}

        if not stream:
            parts, thoughts, calls, done = [], [], None, {}
            ev = first
            while True:
                if "progress" in ev:
                    pass
                elif "delta" in ev:
                    parts.append(ev["delta"])
                elif "reasoning" in ev:
                    thoughts.append(ev["reasoning"])
                elif "tool_calls" in ev:
                    calls = ev["tool_calls"]
                elif "done" in ev:
                    done = ev
                    break
                try:
                    ev = await anext(events)
                except StopAsyncIteration:
                    break
            message = {"role": "assistant", "content": "".join(parts) or (None if calls else "")}
            if thoughts:
                message["reasoning_content"] = "".join(thoughts)
            if calls:
                message["tool_calls"] = calls
            return web.json_response({
                "id": cid, "object": "chat.completion", "created": created, "model": done.get("model", model_name),
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if calls else done.get("finish_reason", "stop")}],
                "usage": {"prompt_tokens": 0, "completion_tokens": done.get("tokens", len(parts)),
                          "total_tokens": done.get("tokens", len(parts))},
                "system_fingerprint": "mycel-" + __version__,
                "mesh": mesh_meta(done),
            })

        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)

        def chunk(delta, finish=None, extra=None):
            c = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if extra:
                c.update(extra)
            return b"data: " + json.dumps(c).encode() + b"\n\n"

        saw_calls = False
        try:
            await resp.write(chunk({"role": "assistant", "content": ""}))
            ev = first
            while True:
                if "progress" in ev:
                    pass
                elif "delta" in ev:
                    await resp.write(chunk({"content": ev["delta"]}))
                elif "reasoning" in ev:
                    await resp.write(chunk({"reasoning_content": ev["reasoning"]}))
                elif "tool_calls" in ev:
                    saw_calls = True
                    await resp.write(chunk({"tool_calls": [{**c, "index": i} for i, c in enumerate(ev["tool_calls"])]}))
                elif "done" in ev:
                    await resp.write(chunk({}, "tool_calls" if saw_calls else ev.get("finish_reason", "stop"),
                                           {"mesh": mesh_meta(ev)}))
                    break
                try:
                    ev = await anext(events)
                except StopAsyncIteration:
                    break
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except ConnectionResetError:
            pass
        except RouteError as exc:
            await resp.write(b"data: " + json.dumps({"error": {"message": str(exc)}}).encode() + b"\n\n")
        return resp

import { api } from "./api.js";
import { state, on, refreshMesh, modelName } from "./state.js";
import { h, icon, nodeAvatar, fmt, compact, short, regionName, tooltip, toast, modal } from "./ui.js";
import { layerStrip, swarmBadge } from "./path.js";
import { cockpit } from "./cockpit.js";
import { globe } from "./gl.js";

const GLOBE_COLOR = { ok: "#3fd7c3", busy: "#f5b84d", client: "#6ea8ff", slice: "#ff5fa8" };

const W = 1000, TOP = 85, BOT = -60, H = W * (TOP - BOT) / 360;
export const proj = (lat, lon) => [(lon + 180) / 360 * W, (TOP - lat) / (TOP - BOT) * H];

function nodeState(n) {
  if (n.role !== "server" && n.stages?.length) return { key: "slice", label: "Hosting a slice", color: "var(--swarm-1)" };
  if (n.role !== "server") return { key: "client", label: "Using the mesh", color: "var(--series-3)" };
  if (n.paused) return { key: "busy", label: "Paused", color: "var(--series-2)" };
  const busy = (n.budget_s ?? 0) < 1 || (n.active_jobs ?? 0) >= (n.max_concurrent ?? 1);
  return busy ? { key: "busy", label: "At its limit", color: "var(--series-2)" } : { key: "ok", label: "Accepting work", color: "var(--series-1)" };
}

export function mapSvg(label = "Map of mesh nodes that chose to share a coarse location") {
  const svg = h("svg:svg", { class: "map", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
  const g = h("svg:g");
  for (let lon = -150; lon <= 150; lon += 30) { const [x] = proj(0, lon); g.appendChild(h("svg:line", { x1: x, x2: x, y1: 0, y2: H, class: "gl" })); }
  for (let lat = -60; lat <= 80; lat += 20) { const [, y] = proj(lat, 0); g.appendChild(h("svg:line", { x1: 0, x2: W, y1: y, y2: y, class: "gl" })); }
  svg.appendChild(g);
  const land = h("svg:g");
  for (const ring of window.LAND_RINGS || []) {
    let d = "";
    for (let i = 0; i < ring.length; i += 2) {
      const [x, y] = proj(Math.max(BOT, Math.min(TOP, ring[i + 1])), ring[i]);
      d += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }
    land.appendChild(h("svg:path", { d: d + "Z", class: "land" }));
  }
  svg.appendChild(land);
  const layer = h("svg:g");
  svg.appendChild(layer);
  return { svg, layer };
}

export function render(container) {
  const offs = [];
  const tiles = h("div", { class: "tiles" });
  const theGlobe = globe({ height: 400,
    points: () => state.nodes.filter((n) => n.lat != null).map((n) => ({ lat: n.lat, lon: n.lon, self: n.self,
      color: n.stages?.length && n.role !== "server" ? GLOBE_COLOR.slice : GLOBE_COLOR[nodeState(n).key] || GLOBE_COLOR.client })),
    links: () => {   // each swarm, drawn in layer order: the path a swarm request takes
      const located = state.nodes.filter((n) => n.lat != null), out = [];
      const byModel = {};
      located.forEach((n, i) => (n.stages || []).forEach((st) => (byModel[st.m] ||= []).push([st.s, i])));
      for (const list of Object.values(byModel)) {
        list.sort((a, b) => a[0] - b[0]);
        for (let k = 1; k < list.length; k++) if (list[k][1] !== list[k - 1][1]) out.push([list[k - 1][1], list[k][1], "#ff5fa8"]);
      }
      return out;
    } });
  const legendExtra = h("span", { class: "muted" });
  const modelsBody = h("tbody");
  const nodesBody = h("tbody");
  const you = h("div");
  const swarmsBody = h("div");
  const impact = h("div");

  container.append(h("div", { class: "view" }, h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Network"),
      h("p", {}, "Your node, and everyone on the mesh — live. No owner, no central server.")),
    h("div", { class: "spacer" })),
    cockpit(),
    tiles,
    h("div", { class: "card", style: { marginBottom: "14px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } }, h("h2", { style: { margin: 0 } }, "Swarms"), swarmBadge("one model, many GPUs")),
      h("p", { style: { marginTop: "6px" } }, "Models too big for any one member, assembled from slices of layers. Nobody assigns the slices: each host reads this map and takes the thinnest stretch — so the swarm fills its gaps as members join, gains replicas as more arrive, and heals when someone leaves."),
      swarmsBody),
    h("div", { class: "grid cols-2" },
      h("div", { class: "grid" },
        h("div", { class: "card globe-card" }, h("h2", {}, "Where the mesh is"), h("p", {}, "Members who chose to share a coarse cell — never a real position. Pink arcs trace each swarm in layer order. Drag to spin."),
          theGlobe,
          h("div", { class: "legend" },
            h("span", {}, h("i", { style: { background: "#3fd7c3" } }), "Accepting work"),
            h("span", {}, h("i", { style: { background: "#f5b84d" } }), "Busy or at its limit"),
            h("span", {}, h("i", { style: { background: "#6ea8ff" } }), "Using the mesh"),
            h("span", {}, h("i", { style: { background: "#ff5fa8" } }), "Hosting a swarm slice"),
            legendExtra)),
        h("div", { class: "card" }, h("h2", {}, "Models"), h("p", {}, "What you can chat with right now."),
          h("div", { class: "scroll-x" }, h("table", { class: "table" },
            h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", {}, "Best tier"), h("th", { class: "num" }, "Nodes"), h("th", { class: "num" }, "Ready"), h("th", { class: "num" }, "Top speed"))),
            modelsBody)))),
      h("div", { class: "grid", style: { alignContent: "start" } }, impact)),
    h("div", { class: "card", style: { marginTop: "14px" } }, h("h2", {}, "Members online"), h("p", {}, "Node ids are pseudonymous keys. Speeds marked * are self-reported until your node measures them."),
      h("div", { class: "scroll-x" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Member", "State", "Hardware", "Serves", "tok/s", "Share", "Jobs", "Region", "RTT", ""].map((t, i) =>
          h("th", { class: [4, 5, 6, 8].includes(i) ? "num" : "" }, t)))),
        nodesBody))))));

  function tile(k, ic, v, unit, s) {
    return h("div", { class: "card tile" }, h("div", { class: "k" }, icon(ic, "sm"), k), h("div", { class: "v" }, v, unit ? h("small", {}, unit) : null), h("div", { class: "s" }, s));
  }

  function drawTiles() {
    const s = state.stats;
    if (!s) return;
    tiles.replaceChildren(
      tile("Members online", "users", fmt(s.active_nodes), "", `${s.client_nodes} using · ${s.serving_nodes} sharing GPUs`),
      tile("Ready for work", "bolt", fmt(s.available_now), "", `of ${s.serving_nodes} serving nodes`),
      tile("Donated capacity", "spark", fmt(s.sustained_tokens_per_s), "tok/s", "sustained, within everyone's limits"),
      tile("GPU memory", "cpu", fmt(s.accel_memory_gb), "GB", Object.entries(s.tiers).sort().map(([t, c]) => `${t} ×${c}`).join(" · ") || "no servers yet"),
      tile("Regions", "globe", fmt(s.regions), "", `${s.located_nodes} members share a location`),
      tile("Refused", "shield", fmt(s.adverts_rejected), "", "messages without a valid identity"));
  }

  function drawMap() {   // the globe animates itself; just keep the legend honest
    const hidden = state.nodes.filter((n) => !n.location).length;
    legendExtra.textContent = hidden ? `${hidden} member${hidden === 1 ? "" : "s"} keep their location private` : "";
  }

  function serves(n) {
    const out = n.models.length ? [n.models.map(modelName).join(", ")] : [];
    for (const st of n.stages || []) {
      out.push(h("span", { class: "badge swarm", style: { marginLeft: out.length ? "6px" : 0 }, title: `Hosts layers ${st.s + 1}–${st.e} of ${modelName(st.m)}` },
        icon("layers", "sm"), `${modelName(st.m)} · layers ${st.s + 1}–${st.e}`));
    }
    return out.length ? out : "–";
  }

  function drawSwarms() {
    const sw = state.stats?.swarms || {};
    const mine = state.status?.swarm;
    const ids = Object.keys(sw).sort((a, b) => sw[b].layers - sw[a].layers);
    if (!ids.length) {
      swarmsBody.replaceChildren(h("div", { class: "hint" }, "No swarms yet. Anyone with a GPU can start one: Models → Host a slice."));
      return;
    }
    swarmsBody.replaceChildren(...ids.map((id) => {
      const s = sw[id];
      const me = mine && mine.model === id && mine.span ? mine.span : null;
      return h("div", { class: "swarm-card" },
        h("div", { class: "swarm-head" }, h("b", {}, modelName(id)),
          s.complete ? h("span", { class: "badge ok" }, icon("check", "sm"), "Assembled") : h("span", { class: "badge", style: { color: "var(--warn)" } }, `${Math.round(s.covered * 100)}% assembled`),
          me ? h("span", { class: "badge" }, `You host layers ${me[0] + 1}–${me[1]}`) : null, h("span", { class: "spacer" }),
          h("div", { class: "swarm-facts" }, h("span", {}, h("b", {}, fmt(s.hosts)), " hosts"), h("span", {}, h("b", {}, s.min_replicas > 1 ? `≥${s.min_replicas}×` : "1×"), " per layer"),
            s.complete ? h("span", {}, h("b", {}, fmt(s.hops)), " hops") : h("span", {}, "needs ", h("b", {}, `~${fmt(s.need_gb, 0)} GB`)),
            s.est_tps ? h("span", {}, h("b", {}, `~${fmt(s.est_tps, 1)}`), " tok/s") : null)),
        layerStrip({ layers: s.layers, coverage: s.coverage, mine: me, height: 16 }),
        h("div", { class: "strip-axis" }, h("span", {}, "Layer 1"),
          h("span", {}, s.gaps.length ? `Missing: ${s.gaps.slice(0, 3).map(([a, b]) => `${a + 1}–${b}`).join(", ")}` : "Darker = more replicas"), h("span", {}, `Layer ${s.layers}`)));
    }));
  }

  function drawModels() {
    modelsBody.replaceChildren(...state.models.filter((m) => m.nodes > 0).map((m) => h("tr", {},
      h("td", {}, h("b", { style: { fontWeight: 600 } }, m.name || m.id)), h("td", {}, m.tier || "–"), h("td", { class: "num" }, fmt(m.nodes)),
      h("td", { class: "num" }, fmt(m.available)), h("td", { class: "num" }, `${fmt(m.tps, 0)} tok/s`))));
    if (!state.models.some((m) => m.nodes > 0)) modelsBody.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "muted" }, "No models online yet.")));
  }

  function drawNodes() {
    const founder = state.membership?.founder, global = state.membership?.kind === "global";
    const rows = [...state.nodes].sort((a, b) => (b.self - a.self) || (a.role === b.role ? a.node_id.localeCompare(b.node_id) : a.role === "server" ? -1 : 1));
    nodesBody.replaceChildren(...rows.map((n) => {
      const st = nodeState(n);
      const serving = n.role === "server";
      const remove = n.self ? null
        : founder ? h("button", { class: "btn ghost sm danger", onclick: () => confirmRemove(n) }, "Remove")
        : global ? h("button", { class: "btn ghost sm", onclick: () => confirmBlock(n), title: "Stop using and serving this member on your device" }, "Block") : null;
      return h("tr", { class: n.self ? "me" : "" },
        h("td", {}, h("div", { style: { display: "flex", alignItems: "center", gap: "9px" } }, nodeAvatar(n.node_id, 24),
          h("span", { class: "mono" }, short(n.node_id)), n.self ? h("span", { class: "badge" }, "You") : null, n.founder && n.mesh_kind === "private" ? h("span", { class: "badge grad" }, "Founder") : null)),
        h("td", {}, h("span", { class: "badge" }, h("span", { class: "dot", style: { background: st.color } }), st.label)),
        h("td", {}, n.device ? `${n.device}` : h("span", { class: "muted", title: "This member keeps their hardware private" }, "Private"), h("span", { class: "muted" }, ` · ${n.tier || "–"}${n.simulated ? " sim" : ""}`)),
        h("td", {}, serves(n)),
        h("td", { class: "num" }, serving ? (n.tps_observed ? fmt(n.tps_observed, 1) : `${fmt(n.tps_claimed, 1)}*`) : "–"),
        h("td", { class: "num" }, serving ? `${Math.round((n.max_share || 0) * 100)}%` : "–"),
        h("td", { class: "num" }, serving ? `${n.active_jobs}/${n.max_concurrent}` : "–"),
        h("td", {}, n.location ? regionName(n.location) : h("span", { class: "muted" }, "Hidden")),
        h("td", { class: "num" }, n.self ? "–" : `${fmt(n.rtt_ms, 0)} ms`),
        h("td", { class: "num" }, remove));
    }));
  }

  function confirmRemove(n) {
    modal({ title: `Remove member ${short(n.node_id)}?`, subtitle: "Their certificate is revoked across the mesh, along with anyone they invited. This can't be undone.",
      actions: [{ label: "Cancel", kind: "ghost" }, { label: "Remove member", kind: "primary", onClick: async () => {
        try { await api.post("/api/membership/revoke", { node_id: n.node_id }); toast("Member removed"); refreshMesh(); }
        catch (e) { toast(e.message, "bad"); }
      } }] });
  }

  function confirmBlock(n) {
    modal({ title: `Block member ${short(n.node_id)}?`, subtitle: "Your node stops sending them work and refuses theirs. This only affects your device — the global mesh has no owner who can evict anyone.",
      actions: [{ label: "Cancel", kind: "ghost" }, { label: "Block", kind: "primary", onClick: async () => {
        try { await api.post("/api/membership/block", { node_id: n.node_id }); toast("Blocked on this device"); refreshMesh(); }
        catch (e) { toast(e.message, "bad"); }
      } }] });
  }

  function drawYou() {
    const st = state.status;
    if (!st) return;
    const g = st.governor, hw = st.hardware || {};
    const pct = g.capacity_seconds ? Math.min(100, 100 * g.available_seconds / g.capacity_seconds) : 0;
    you.replaceChildren(h("div", { class: "card" },
      h("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "14px" } }, nodeAvatar(st.node_id, 40),
        h("div", {}, h("div", { style: { fontWeight: 620 } }, "Your node"), h("div", { class: "muted", style: { fontSize: "12.5px" } },
          `${hw.device_name || "–"} · ${fmt(hw.accel_memory_gb, 1)} GB · ${hw.tier || "–"}${hw.simulated ? " (simulated)" : ""}`))),
      st.serving ? [
        h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "12.5px", marginBottom: "6px" } },
          h("span", { class: "dim" }, g.limits.paused ? "Paused" : "Budget available now"), h("span", { class: "muted" }, `${fmt(g.available_seconds, 1)} / ${fmt(g.capacity_seconds, 1)} GPU-s`)),
        h("div", { class: "meter" }, h("i", { style: { width: pct + "%" } })),
        h("div", { class: "hint", style: { marginTop: "8px" } }, `Hard limit: ${Math.round(g.limits.max_share * 100)}% of GPU time · ${g.admitted} jobs served · ${g.rejected} refused`),
        h("div", { style: { display: "flex", gap: "8px", marginTop: "14px" } },
          h("button", { class: "btn sm", onclick: () => (location.hash = "#/settings/sharing") }, icon("sliders", "sm"), "Adjust"),
          h("button", { class: "btn sm", onclick: async () => { await api.post("/api/limits", { paused: !g.limits.paused }); refreshMesh(); } },
            icon(g.limits.paused ? "play" : "pause", "sm"), g.limits.paused ? "Resume" : "Pause")),
      ] : h("div", { class: "hint" }, st.eligibility_problems.join("; "))));
  }

  function drawImpact() {
    const imp = state.impact;
    if (!imp) return;
    const max = Math.max(1, ...imp.series.map((d) => d.given));
    const bars = h("div", { class: "bars", role: "img", "aria-label": "Tokens you gave per day, last 14 days" },
      imp.series.map((d) => { const b = h("div", { style: { height: Math.max(2, (d.given / max) * 100) + "%" }, tabindex: 0 });
        tooltip(b, () => h("div", {}, h("b", {}, new Date(d.day + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })),
          h("div", { class: "dim" }, `${fmt(d.given)} tokens given`), h("div", { class: "dim" }, `${fmt(d.received)} tokens received`)));
        return b; }));
    const ratio = imp.total_received ? imp.total_given / imp.total_received : null;
    impact.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Your impact"), h("p", {}, "What your GPU has given the community."),
      h("div", { style: { display: "flex", gap: "22px" } },
        h("div", {}, h("div", { style: { fontSize: "26px", fontWeight: 650 } }, compact(imp.total_given)), h("div", { class: "hint" }, "tokens given")),
        h("div", {}, h("div", { style: { fontSize: "26px", fontWeight: 650 } }, fmt(imp.people_helped)), h("div", { class: "hint" }, "members helped")),
        h("div", {}, h("div", { style: { fontSize: "26px", fontWeight: 650 } }, ratio ? `${ratio.toFixed(1)}×` : "–"), h("div", { class: "hint" }, "given vs. used"))),
      bars, h("div", { class: "bars-axis" }, h("span", {}, "14 days ago"), h("span", {}, "Today"))));
  }

  const drawAll = () => { drawTiles(); drawSwarms(); drawMap(); drawModels(); drawNodes(); };
  offs.push(on("mesh", drawAll), on("impact", drawImpact));
  drawAll(); drawImpact();
  return () => offs.forEach((f) => f());
}

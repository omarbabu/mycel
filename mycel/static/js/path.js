// "How this reply was made": the path a request took through the mesh, made visible.
// Swarm replies show every stage (who computed which layers, compute vs. network time),
// failovers, cross-checks and speculation. Everything shown comes from the node's own trace;
// each member's device and location appear only if that member chose to share them.
import { state, modelName } from "./state.js";
import { h, icon, nodeAvatar, fmt, short, regionName, modal, tooltip } from "./ui.js";
import { mapSvg, proj } from "./network.js";
import { flag } from "./fx.js";

const HOP_COLORS = ["#6ea8ff", "#8b7bff", "#c77dff", "#ff6fae", "#ff8a5b", "#f5b84d", "#3fd7c3", "#5fd38d", "#4fb3ff", "#a98bff"];
export const hopColor = (i) => HOP_COLORS[i % HOP_COLORS.length];

export function swarmBadge(text = "Swarm", cls = "") {
  return h("span", { class: `badge swarm ${cls}` }, icon("layers", "sm"), text);
}

// A strip of the model's layers. `segments` = [{a, b, color, label}] or a coverage array.
export function layerStrip({ layers, segments, coverage, mine, height = 14, live = false }) {
  const strip = h("div", { class: `layer-strip ${live ? "live" : ""} ${coverage ? "cov" : ""}`, style: { height: height + "px" }, role: "img",
    "aria-label": segments ? `${layers} layers split into ${segments.length} stages` : `Coverage of ${layers} layers` });
  if (segments) {
    for (const s of segments) {
      const seg = h("i", { style: { flex: `${s.b - s.a} 0 0`, background: s.color, opacity: s.pending ? 0.18 : 1 }, class: s.pending ? "pending" : "" });
      if (s.label) tooltip(seg, () => h("div", {}, s.label));
      strip.appendChild(seg);
    }
  } else if (coverage) {
    const max = Math.max(1, ...coverage);
    coverage.forEach((c, i) => {
      const cell = h("i", { class: c ? "" : "gap", style: { flex: "1 0 0",
        background: c ? `color-mix(in srgb, var(--swarm-1) ${Math.round(35 + 65 * c / max)}%, var(--panel-3))` : null } });
      if (mine && i >= mine[0] && i < mine[1]) cell.classList.add("mine");
      strip.appendChild(cell);
    });
  }
  return strip;
}

export const layersText = ([a, b]) => (b - a === 1 ? `Layer ${b}` : `Layers ${a + 1}–${b}`);
const where = (hop) => hop.location ? regionName(hop.location) : null;
const nodeLabel = (hop) => hop.self ? "You" : `Node ${short(hop.node)}`;

function hopCard(hop, i, n) {
  const color = hopColor(i);
  const card = h("div", { class: "hop", style: { "--hop": color } },
    h("div", { class: "hop-top" }, nodeAvatar(hop.node, 26), h("div", {},
      h("div", { class: "hop-name" }, nodeLabel(hop)),
      h("div", { class: "hop-sub" }, hop.layers ? layersText(hop.layers) : "Whole model"))),
    h("div", { class: "hop-meta" },
      h("span", { title: hop.device ? "Shared by this member" : "This member keeps their hardware private" },
        icon("cpu", "sm"), hop.device ? `${hop.device}${hop.simulated ? " (sim)" : ""}` : h("span", { class: "muted" }, hop.tier ? `${hop.tier} · private` : "Private")),
      h("span", {}, icon("pin", "sm"), where(hop) || h("span", { class: "muted" }, "Location private"))),
    hop.compute_ms != null ? h("div", { class: "hop-ms" },
      h("span", {}, h("b", {}, `${fmt(hop.compute_ms, hop.compute_ms < 10 ? 1 : 0)} ms`), " compute"),
      h("span", {}, h("b", {}, `${fmt(hop.net_ms, 0)} ms`), " network")) : null);
  return card;
}

function routeDiagram(path) {
  const row = h("div", { class: "route" });
  const bookend = (t, d) => h("div", { class: "hop end" }, h("div", { class: "hop-top" }, h("span", { class: "ic-tile" }, icon("lock", "sm")),
    h("div", {}, h("div", { class: "hop-name" }, t), h("div", { class: "hop-sub" }, d))));
  if (path.mode === "swarm") row.appendChild(bookend("You", "Tokenize · embed"));
  else row.appendChild(bookend("You", "Your messages"));
  path.hops.forEach((hop, i) => {
    row.appendChild(h("div", { class: "wire" }, h("span", {}, i && path.express?.steps ? "stage → stage" : hop.transport === "relay" ? "relay · E2E" : hop.transport === "local" ? "on device" : "direct · E2E")));
    row.appendChild(hopCard(hop, i, path.hops.length));
  });
  row.appendChild(h("div", { class: "wire" }, h("span", {}, "")));
  row.appendChild(bookend("You", path.mode === "swarm" ? "LM head · sample · text" : "Reply"));
  return row;
}

function routeMap(path) {
  const { svg, layer } = mapSvg("Map of the route this reply took (members who share a location)");
  const pts = [];
  const me = state.nodes.find((n) => n.self);
  if (me?.lat != null) pts.push({ ...me, lat: me.lat, lon: me.lon, label: "You", i: -1 });
  path.hops.forEach((hop, i) => { if (hop.lat != null) pts.push({ ...hop, label: nodeLabel(hop), i }); });
  for (let k = 1; k < pts.length; k++) {
    const [x1, y1] = proj(pts[k - 1].lat, pts[k - 1].lon), [x2, y2] = proj(pts[k].lat, pts[k].lon);
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - Math.max(12, Math.hypot(x2 - x1, y2 - y1) * 0.22);
    layer.appendChild(h("svg:path", { d: `M${x1} ${y1} Q${mx} ${my} ${x2} ${y2}`, class: "arc", stroke: hopColor(Math.max(0, pts[k].i)),
      style: { animationDelay: `${k * 0.18}s` } }));
  }
  pts.forEach((p) => {
    const [x, y] = proj(p.lat, p.lon);
    layer.appendChild(h("svg:circle", { cx: x, cy: y, r: p.i < 0 ? 7 : 6, class: "node", fill: p.i < 0 ? "var(--text)" : hopColor(p.i) }));
  });
  if (pts.length) {  // zoom to the route
    const xy = pts.map((p) => proj(p.lat, p.lon));
    const xs = xy.map((p) => p[0]), ys = xy.map((p) => p[1]);
    const w = Math.max(220, Math.max(...xs) - Math.min(...xs) + 120), hgt = w * 0.5;
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
    svg.setAttribute("viewBox", `${cx - w / 2} ${cy - hgt / 2} ${w} ${hgt}`);
  }
  const hidden = path.hops.filter((hop) => hop.lat == null).length;
  return h("div", {}, svg, h("div", { class: "hint", style: { marginTop: "6px" } },
    hidden ? `${hidden} of ${path.hops.length} node${path.hops.length === 1 ? "" : "s"} keep their location private — they aren't drawn.` : "Every node on this route shares a coarse location."));
}

function eventRow(ev, path) {
  if (ev.kind === "failover" || ev.kind === "moved") {
    return h("div", { class: "ev bad" }, icon("refresh", "sm"), h("div", {},
      h("b", {}, ev.kind === "moved" ? `Layers ${ev.layers[0] + 1}–${ev.layers[1]} were re-placed mid-reply` : `Node ${short(ev.node)} dropped out at ${fmt(ev.at_ms / 1000, 1)} s`),
      h("div", { class: "dim" }, `Re-routed layers ${ev.layers[0] + 1}–${ev.layers[1]} to ${ev.replaced_by.map(short).join(" + ")} and replayed ${fmt(ev.replayed_tokens)} tokens to rebuild its memory — ${fmt(ev.recovery_ms)} ms, no restart.`)));
  }
  if (ev.kind === "crosscheck") {
    return h("div", { class: `ev ${ev.ok ? "good" : "bad"}` }, icon(ev.ok ? "shieldCheck" : "alert", "sm"), h("div", {},
      h("b", {}, ev.ok ? `Layers ${ev.layers[0] + 1}–${ev.layers[1]} cross-checked: match` : `Layers ${ev.layers[0] + 1}–${ev.layers[1]} failed a cross-check`),
      h("div", { class: "dim" }, ev.ok
        ? `Node ${short(ev.checker)} independently recomputed node ${short(ev.node)}'s work on your prompt and got the same activations.`
        : `${ev.liar ? `Node ${short(ev.liar)} returned wrong math` : "Two replicas disagreed"}${ev.replaced ? " and was replaced before your reply started" : ""}. It's down-ranked on your device.`)));
  }
  return null;
}

export function summary(done) {
  const p = done.path || {};
  if (p.mode === "swarm") return `Swarm · ${done.swarm_nodes || p.hops.length} GPUs`;
  if (p.mode === "local") return "Your device";
  return `Node ${short(done.served_by)}`;
}

export function openPath(done) {
  const path = done.path;
  if (!path) return;
  flag("opened-path");
  const swarm = path.mode === "swarm";
  const segments = swarm ? path.hops.map((hop, i) => ({ a: hop.layers[0], b: hop.layers[1], color: hopColor(i),
    label: `${nodeLabel(hop)} · layers ${hop.layers[0] + 1}–${hop.layers[1]}` })) : null;
  const pills = [
    swarm ? swarmBadge(`Swarm · ${path.hops.length} GPUs`) : h("span", { class: "badge" }, icon(path.mode === "local" ? "lock" : "globe", "sm"), path.mode === "local" ? "On your device" : "One mesh GPU"),
    h("span", { class: "badge" }, modelName(done.model)),
    done.tokens_per_s ? h("span", { class: "badge" }, `${fmt(done.tokens_per_s, 1)} tok/s`) : null,
    done.latency_ms != null ? h("span", { class: "badge" }, `First token ${fmt(done.latency_ms / 1000, 2)} s`) : null,
    path.e2e ? h("span", { class: "badge ok" }, icon("lock", "sm"), "End-to-end encrypted") : null,
    swarm && path.text_stays_local ? h("span", { class: "badge ok" }, icon("eyeOff", "sm"), "No node saw your text") : null,
    swarm && path.express?.steps ? h("span", { class: "badge", title: "Each stage handed its output straight to the next one (and a copy back to you): N+1 network legs per token instead of 2N" }, icon("bolt", "sm"), "Express lane") : null,
  ];
  const spec = path.speculative;
  const events = (path.events || []).map((ev) => eventRow(ev, path)).filter(Boolean);
  if (path.crosscheck && !(path.events || []).some((e) => e.kind === "crosscheck")) events.push(eventRow(path.crosscheck, path));
  if (spec && spec.proposed) {
    events.push(h("div", { class: "ev good" }, icon("bolt", "sm"), h("div", {},
      h("b", {}, `Speculative decoding: ${fmt(100 * spec.accepted / spec.proposed)}% of guesses accepted`),
      h("div", { class: "dim" }, `A small model on your device (${modelName(spec.draft)}) drafted ${fmt(spec.proposed)} tokens; the swarm verified them in ${fmt(spec.rounds)} round trips — ${fmt((done.tokens || 0) / Math.max(1, spec.rounds), 1)} tokens per trip instead of 1.`))));
  }
  if (!events.length && swarm) events.push(h("div", { class: "ev good" }, icon("check", "sm"), h("div", {}, h("b", {}, "Clean run"), h("div", { class: "dim" }, "Every stage answered first time."))));
  const privacy = swarm
    ? [["eyeOff", "Your device turned text into numbers (embeddings) and turned the final numbers back into text. No node in the chain received a single word or token id."],
       ["layers", "Each node saw only the activations entering its layers. Determined adversaries can try to invert activations, so for sensitive work use a model on your own GPU."],
       ["lock", "Every hop was sealed end to end (HPKE). Relays forwarded ciphertext and learned only who talked to whom, and when."]]
    : path.mode === "local"
      ? [["lock", "Generated on this device. Your conversation never left it."]]
      : [["globe", `Node ${short(done.served_by)} read this conversation to answer it, and signed a receipt for exactly this text.`],
         ["lock", "Sealed end to end in transit; relays saw only ciphertext."]];
  modal({
    title: "How this reply was made", wide: true, iconEl: h("span", { class: "ic-tile" }, icon("route")),
    subtitle: swarm ? `${modelName(done.model)} has ${path.layers} layers. ${path.hops.length} members each computed a slice, in order.` : null,
    body: h("div", { class: "pathview" },
      h("div", { class: "pills" }, pills.filter(Boolean)),
      h("h4", {}, "The route"), routeDiagram(path),
      swarm ? [h("h4", {}, `Who computed which of the ${path.layers} layers`), layerStrip({ layers: path.layers, segments, height: 18 }),
        h("div", { class: "strip-axis" }, h("span", {}, "Layer 1"), h("span", {}, `Layer ${path.layers}`))] : null,
      h("div", { class: "grid cols-2", style: { marginTop: "18px", alignItems: "start" } },
        h("div", {}, h("h4", {}, "On the map"), routeMap(path)),
        h("div", {}, h("h4", {}, "What happened"), h("div", { class: "evs" }, events.length ? events : h("div", { class: "hint" }, "One node answered directly.")),
          h("h4", {}, "Who saw what"), h("div", {}, privacy.map(([ic, t]) => h("div", { class: "perm" }, icon(ic, "sm"), t)))))),
  });
}

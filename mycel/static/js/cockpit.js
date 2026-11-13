// The cockpit: your node at a glance, and every switch that controls it.
// Three ways to take part, one module each — your model (private or shared), a swarm slice,
// and your limit — around a reactor that visibly works harder when the mesh is using you.
import { api } from "./api.js";
import { state, on, refreshMesh, refreshImpact, modelName } from "./state.js";
import { h, icon, fmt, compact, short, toast, nodeAvatar, modal } from "./ui.js";
import { nodeState, live } from "./nodepanel.js";
import { reactor, meshField, counter, burst, level, flags, flag, announce } from "./fx.js";
import { layerStrip, swarmBadge } from "./path.js";
import { plasmaOrb } from "./gl.js";

// Element.append(null) inserts the text "null"; skip empty slots instead.
const put = (el, ...kids) => el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));

const LEVELS = [[0.1, "Light", "Barely noticeable"], [0.25, "Balanced", "Recommended"], [0.5, "Generous", "Desktops"], [0.75, "Max", "Dedicated rigs"]];
let swarm = null, swarmAt = 0;
async function refreshSwarm(force) {
  if (!force && Date.now() - swarmAt < 2000) return swarm;
  swarmAt = Date.now();
  try { swarm = await api.get("/api/swarm"); } catch { /* offline */ }
  return swarm;
}

async function act(fn, msg, from) {
  try { await fn(); await Promise.all([refreshMesh(), refreshSwarm(true)]); if (msg) toast(msg); if (from) burst(from); }
  catch (e) { toast(e.message, "bad"); }
}

export function achievements() {
  const st = state.status || {}, imp = state.impact || {}, f = flags(), sw = st.swarm || {};
  const given = imp.total_given || 0, people = imp.people_helped || 0;
  if (st.serving) flag("ran-model");
  if (sw.model && sw.span) flag("hosted-slice");
  return [
    { id: "joined", icon: "globe", title: "On the mesh", d: "Joined the global mesh", done: !!st.member && (st.network?.mode !== "internet" || !!st.network?.online) },
    { id: "first-light", icon: "bolt", title: "First light", d: "Ran a model on your own GPU", done: !!st.serving || !!f["ran-model"] },
    { id: "open-hand", icon: "heart", title: "Open hand", d: "Shared your model with the mesh", done: !!st.sharing || given > 0 },
    { id: "swarm-host", icon: "layers", title: "Swarm host", d: "Held a slice of a giant model", done: !!(sw.model && sw.span) || !!f["hosted-slice"] },
    { id: "rider", icon: "route", title: "Rode the swarm", d: "Got a reply from a swarm", done: !!f["swarm-reply"] },
    { id: "pathfinder", icon: "eye", title: "Pathfinder", d: "Inspected a reply's path", done: !!f["opened-path"] },
    { id: "helper", icon: "users", title: "Helper", d: "Helped your first person", done: people >= 1 },
    { id: "pillar", icon: "star", title: "Pillar", d: "Helped 25 people", done: people >= 25 },
    { id: "10k", icon: "spark", title: "10k club", d: "Gave 10,000 tokens", done: given >= 10000 },
  ];
}

// ---------------------------------------------------------------- the three modules
function modelModule() {
  const st = state.status, srv = st.model_server || {}, box = h("div", { class: "mod" });
  const head = (ic, t, badge) => h("div", { class: "mod-h" }, h("span", { class: "mod-ic" }, icon(ic)), h("b", {}, t), h("span", { class: "spacer" }), badge || null);
  const hw = st.hardware || {};
  const canRun = hw.accelerator && hw.accelerator !== "cpu" && hw.accel_memory_gb >= 8;
  if (["downloading", "loading", "ready"].includes(srv.status) && !st.serving) {
    put(box, head("cpu", "Your model", h("span", { class: "badge" }, h("span", { class: "spin" }), srv.status === "downloading" ? "Downloading" : "Loading")),
      h("div", { class: "mod-big" }, modelName(srv.model_id)),
      h("div", { class: "meter glow-meter" }, h("i", { style: { width: `${Math.round((srv.progress ?? 0.6) * 100)}%` } })),
      h("div", { class: "mod-note" }, "Keeps going in the background — chat with the mesh meanwhile."));
    return box;
  }
  if (!st.serving) {
    put(box, head("cpu", "Your model", h("span", { class: "badge" }, "Off")),
      h("div", { class: "mod-big muted" }, canRun ? "Nothing running yet" : "No GPU for whole models"),
      h("div", { class: "mod-note" }, canRun ? "Run a model on your GPU — keep it private, or share it with the mesh." : "Whole models need a GPU with 8 GB+. You can still use everything on the mesh."),
      srv.status === "error" && srv.error ? h("div", { class: "mod-note bad" }, srv.error) : null,
      canRun ? h("button", { class: "btn glow sm", onclick: () => (location.hash = "#/models") }, icon("bolt", "sm"), "Pick a model") : null);
    return box;
  }
  // One switch, two states. Read from the limit itself, so it's right even against an older node.
  const shared = st.sharing ?? !st.governor.limits.paused;
  const sw = h("button", { class: `power ${shared ? "on" : ""}`, role: "switch", "aria-checked": String(shared),
    "aria-label": "Share my model with the mesh",
    onclick: (e) => {
      const next = !sw.classList.contains("on");
      sw.classList.toggle("on", next); sw.setAttribute("aria-checked", String(next));   // instant feedback
      box.classList.toggle("lit-share", next); box.classList.toggle("lit-private", !next);
      label.replaceChildren(...labelFor(next));
      act(() => api.post("/api/limits", { paused: !next }), next ? "Shared — your GPU is powering the mesh" : "Private — your model is only for you",
        next ? e.currentTarget.querySelector(".pw-knob") : null);
    } },
  h("span", { class: "pw-track" }, h("span", { class: "pw-glow" }), h("span", { class: "pw-knob" }, icon("globe", "sm"), icon("lock", "sm"))));
  const labelFor = (on) => [h("b", {}, on ? "Shared with the mesh" : "Private — only you"),
    h("small", {}, on ? "Members can use it within your limit" : "Invisible to the mesh, even its name")];
  const label = h("div", { class: "pw-label" }, ...labelFor(shared));
  box.classList.add(shared ? "lit-share" : "lit-private");
  put(box, head("cpu", "Your model", h("span", { class: "badge ok" }, h("span", { class: "dot live" }), "Active")),
    h("div", { class: "mod-big" }, modelName(st.backend.models[0]), h("small", {}, `${fmt(st.backend.tokens_per_s)} tok/s on your GPU`)),
    h("div", { class: "power-row" }, sw, label),
    h("div", { class: "mod-acts" }, h("button", { class: "btn sm ghost", onclick: () => (location.hash = "#/models") }, "Change model"),
      h("button", { class: "btn sm ghost", onclick: () => act(() => api.post("/api/library/stop", {}), "Model stopped") }, icon("stop", "sm"), "Stop")));
  return box;
}

const draft = { model: null, layers: null };
const ENGINE = { torch: "GPU · fp16", gguf: "CPU · fp32", mock: "simulated" };

function slider(min, max, value, onInput) {
  const input = h("input", { type: "range", class: "layer-range", min, max, value, step: 1, "aria-label": "Layers to host" });
  const paint = () => input.style.setProperty("--fill", `${max > min ? ((input.value - min) / (max - min)) * 100 : 100}%`);
  input.addEventListener("input", () => { paint(); onInput(Number(input.value)); });
  paint();
  return input;
}

function sliceModule() {
  const box = h("div", { class: "mod swarmy" });
  const st = state.status, hs = st.swarm || {}, sw = swarm;
  const rows = (sw?.models || []).filter((r) => !r.fits_whole && (r.capacity_layers || r.capacity_if_free));
  const head = (badge) => h("div", { class: "mod-h" }, h("span", { class: "mod-ic sw" }, icon("layers")), h("b", {}, "Swarm slice"), h("span", { class: "spacer" }),
    sw ? h("span", { class: "eng" }, ENGINE[sw.engine] || "") : null, badge || null);
  if (hs.model) {
    const m = sw?.models.find((x) => x.id === hs.model), mesh = m?.mesh;
    const max = m ? Math.max(m.capacity_layers + (hs.span ? hs.span[1] - hs.span[0] : 0), 1) : 1;
    box.classList.add("lit-swarm");
    const status = hs.status === "downloading" ? h("span", { class: "badge" }, h("span", { class: "spin" }), `Downloading ${Math.round((hs.progress || 0) * 100)}%`)
      : hs.span && hs.status === "ready" ? swarmBadge("Hosting") : h("span", { class: "badge" }, h("span", { class: "spin" }), hs.status === "error" ? "Error" : hs.status === "loading" ? "Loading" : "Placing");
    let want = hs.chosen_layers || (hs.span ? hs.span[1] - hs.span[0] : max);
    const apply = h("button", { class: "btn sm ghost", hidden: true, onclick: () => act(() => api.post("/api/swarm/host", { model: hs.model, layers: want }), `Re-placing with ${want} layers`) }, "Apply");
    const readout = h("span", { class: "range-v" }, `${want} layers`);
    put(box, head(status),
      h("div", { class: "mod-big" }, modelName(hs.model), h("small", {}, hs.span ? `you hold layers ${hs.span[0] + 1}–${hs.span[1]} of ${hs.layers}` : "finding where the swarm needs you…")),
      mesh ? layerStrip({ layers: mesh.layers, coverage: mesh.coverage, mine: hs.span, height: 12 }) : h("div", { class: "layer-strip", style: { height: "12px" } }),
      hs.status === "downloading" ? h("div", { class: "meter glow-meter swarm-meter" }, h("i", { style: { width: `${Math.round((hs.progress || 0) * 100)}%` } })) : null,
      hs.status === "error" ? h("div", { class: "mod-note bad" }, hs.error)
        : h("div", { class: "mod-note" }, hs.span && hs.status === "ready" ? `${fmt(hs.ms_per_layer * (hs.span[1] - hs.span[0]), 1)} ms per token · ${hs.sessions} chat${hs.sessions === 1 ? "" : "s"} now · ${hs.people} people helped` : "Only your layers are fetched — not the whole model."),
      h("div", { class: "range-row" }, slider(1, max, Math.min(want, max), (v) => { want = v; readout.textContent = `${v} layers`; apply.hidden = false; }), readout, apply),
      h("div", { class: "mod-acts" }, h("button", { class: "btn sm ghost", onclick: () => act(() => api.post("/api/swarm/host", { model: null }), "Stopped hosting") }, icon("stop", "sm"), "Stop hosting")));
    return box;
  }
  if (!rows.length) {
    put(box, head(h("span", { class: "badge" }, "Off")), h("div", { class: "mod-big muted" }, sw ? "Not available here" : "…"),
      h("div", { class: "mod-note" }, sw?.models[0]?.problems[0] ? `Swarm slices need a GPU: ${sw.models[0].problems[0]}.` : "Hold a slice of a model too big for any one member."));
    return box;
  }
  const pick = rows.find((r) => r.id === draft.model) || rows.find((r) => r.id === sw.suggested) || rows[0];
  draft.model = pick.id;
  const blocked = pick.capacity_layers < 1;
  const max = Math.max(1, blocked ? pick.capacity_if_free : pick.capacity_layers);
  let n = Math.min(draft.layers || max, max);
  const big = h("div", { class: "mod-big" });
  const cta = h("button", { class: "btn glow-swarm sm" });
  const paintN = () => {
    big.replaceChildren(h("span", { class: "big-n" }, `${n}`), ` of ${pick.layers} layers`,
      h("small", {}, `${fmt(n * pick.layer_gb, 1)} GB of GPU memory${sw.engine === "mock" ? "" : ` · ${fmt(n * pick.download_layer_gb, 1)} GB download`} · up to ${max} fit`));
    cta.replaceChildren(icon("layers", "sm"), blocked ? `Swap model & host ${n} layers` : `Host ${n} layer${n === 1 ? "" : "s"}`);
  };
  paintN();
  cta.onclick = (e) => {
    const go = () => act(() => api.post("/api/swarm/host", { model: pick.id, layers: n, replace_active: blocked }), "Joining the swarm — Mycel is finding your layers", cta);
    if (!blocked) return go();
    modal({ title: `Swap ${modelName(sw.active_model)} for a slice?`, subtitle: `Your model stops, and its memory holds ${n} layers of ${pick.name} instead. Switch back any time.`,
      actions: [{ label: "Cancel", kind: "ghost" }, { label: "Swap & host", kind: "glow", onClick: go }] });
  };
  put(box, head(h("span", { class: "badge" }, "Off")),
    rows.length > 1 ? h("div", { class: "chip-row" }, rows.slice(0, 4).map((r) => h("button", { class: `chip-s ${r.id === pick.id ? "on" : ""}`,
      onclick: () => { draft.model = r.id; draft.layers = null; sig = null; repaint(); } }, r.name))) : null,
    big,
    pick.mesh ? layerStrip({ layers: pick.mesh.layers, coverage: pick.mesh.coverage, height: 12 }) : null,
    h("div", { class: "range-row" }, slider(1, max, n, (v) => { n = draft.layers = v; paintN(); })),
    h("div", { class: "mod-note" }, blocked ? `Your GPU's memory is busy with ${modelName(sw.active_model)} — swapping frees it for the slice.`
      : pick.mesh?.complete ? `The ${pick.name} swarm is live — you'd add a replica (more speed, more resilience).`
      : pick.mesh ? `${Math.round(pick.mesh.covered * 100)}% assembled — you'd help fill the gap.` : "Nobody hosts it yet — you'd be the first slice. You pick how many layers; Mycel picks which."),
    h("div", { class: "mod-acts" }, cta));
  return box;
}

function limitModule() {
  const st = state.status, g = st.governor, today = state.impact?.today || {}, box = h("div", { class: "mod" });
  const cur = g.limits.max_share;
  const pct = g.capacity_seconds ? Math.min(100, (100 * g.available_seconds) / g.capacity_seconds) : 0;
  const contributing = st.sharing || st.swarm?.span;
  put(box, h("div", { class: "mod-h" }, h("span", { class: "mod-ic" }, icon("sliders")), h("b", {}, "Your limit"), h("span", { class: "spacer" }),
      h("span", { class: "badge" }, `${Math.round(cur * 100)}% of GPU time`)),
    h("div", { class: "lvl-seg" }, LEVELS.map(([v, name]) => h("button", { class: Math.abs(cur - v) < 0.001 ? "on" : "",
      onclick: (e) => act(() => api.post("/api/limits", { max_share: v }), `Sharing up to ${Math.round(v * 100)}% of your GPU`, e.currentTarget) },
    h("b", {}, `${Math.round(v * 100)}%`), h("span", {}, name)))),
    h("div", { class: "meter glow-meter", title: "GPU time the mesh may still use in this window" }, h("i", { style: { width: `${contributing ? pct : 0}%` } })),
    h("div", { class: "mod-stats" },
      h("div", {}, counter(today.given || 0, { key: "given" }), h("span", {}, "tokens given today")),
      h("div", {}, counter(today.people || 0, { key: "people" }), h("span", {}, "people helped today")),
      h("div", {}, counter(g.active_jobs + (st.swarm?.sessions || 0), { key: "working" }), h("span", {}, "working now"))),
    h("div", { class: "mod-note" }, "A hard cap for everything you share — your model and your slice. Your own chats are free."));
  return box;
}

// Just the modules (Models page uses these too).
let sig = null, repaint = () => {};
export function modules() {
  const wrap = h("div", { class: "mods" });
  const paint = () => {
    const st = state.status;
    if (!st) return;
    const g = st.governor;
    const next = JSON.stringify([st.serving, st.sharing, st.backend, st.model_server, st.swarm, g.limits, g.active_jobs,
      Math.round(g.available_seconds), state.impact?.today, swarm?.suggested, swarm?.active_model, swarm?.models.map((m) => [m.capacity_layers, m.mesh?.coverage])]);
    if (next === sig && wrap.childElementCount) return;
    if (wrap.contains(document.activeElement) && document.activeElement.type === "range") return;   // don't yank a slider mid-drag
    sig = next;
    const focus = wrap.contains(document.activeElement);
    wrap.replaceChildren(modelModule(), sliceModule(), limitModule());
    if (focus) wrap.querySelector("button.on")?.focus();
  };
  repaint = paint;
  refreshSwarm(true).then(paint);
  live(wrap, () => { paint(); refreshSwarm().then(paint); });
  return wrap;
}

// ---------------------------------------------------------------- the full cockpit (Network page)
export function cockpit() {
  const titleEl = h("div", { class: "ck-title" }), subEl = h("div", { class: "ck-sub" });
  const lvlName = h("b"), lvlNum = h("span", { class: "lvl-n" }), xpFill = h("i"), xpText = h("div", { class: "xp-t" });
  const achRow = h("div", { class: "ach-row" });
  const activity = () => { const st = state.status; if (!st) return 0; const g = st.governor;
    return Math.min(1, (g.active_jobs + (st.swarm?.sessions || 0)) / Math.max(1, g.limits.max_concurrent + 1) + (st.sharing || st.swarm?.span ? 0.2 : 0)); };
  const stateKey = () => nodeState().key;
  const core = plasmaOrb({ size: 230, state: stateKey, activity, fallback: () => reactor({ size: 230, state: stateKey, activity }) });
  const coreLabel = h("div", { class: "core-label" });
  const el = h("section", { class: "cockpit", id: "you" },
    meshField({ density: 0.00005 }),
    h("div", { class: "ck-grid" },
      h("div", { class: "core-wrap" }, core, coreLabel),
      h("div", { class: "ck-right" },
        h("div", { class: "ck-head" }, h("div", {}, h("div", { class: "ck-k" }, "Your node"), titleEl, subEl),
          h("span", { class: "spacer" }), h("div", { class: "ck-id", title: "Your node ID — a random key, not your name" }, nodeAvatar(state.status?.node_id || "", 22), h("span", { class: "mono" }, short(state.status?.node_id)))),
        h("div", { class: "lvl" }, h("div", { class: "lvl-top" }, lvlNum, lvlName, h("span", { class: "spacer" }), xpText), h("div", { class: "xp" }, xpFill)),
        h("div", { class: "ck-ways" }, h("b", {}, "Three ways to take part"), " — keep a model private, share it, or host a slice of a giant. Mix freely; using the mesh is always on."))),
    modules(),
    achRow);
  const paint = () => {
    const s = nodeState(), imp = state.impact || {};
    el.dataset.state = s.key;
    titleEl.textContent = s.title; subEl.textContent = s.sub;
    const st = state.status;
    coreLabel.replaceChildren(st?.sharing ? h("div", {}, counter(st.backend.tokens_per_s, { key: "core-tps" }), h("small", {}, "tok/s shared"))
      : st?.swarm?.span ? h("div", {}, h("b", {}, `${st.swarm.span[1] - st.swarm.span[0]}`), h("small", {}, "layers hosted"))
      : st?.serving ? h("div", {}, icon("lock"), h("small", {}, "private"))
      : s.key === "searching" ? h("div", {}, icon("relay"), h("small", {}, "connecting"))
      : h("div", {}, icon("globe"), h("small", {}, "using the mesh")));
    const L = level(imp.total_given || 0);
    lvlNum.textContent = `Lv ${L.n}`; lvlName.textContent = L.name;
    xpFill.style.width = `${Math.max(3, L.frac * 100)}%`;
    xpText.textContent = L.hi ? `${compact(imp.total_given || 0)} / ${compact(L.hi)} tokens to ${L.next}` : "Max level";
    const achs = achievements();
    achRow.replaceChildren(h("div", { class: "ach-k" }, `Achievements · ${achs.filter((a) => a.done).length}/${achs.length}`),
      ...achs.map((a) => h("div", { class: `ach ${a.done ? "done" : ""}`, title: `${a.title} — ${a.d}` }, h("span", {}, icon(a.icon, "sm")), h("b", {}, a.title))));
    announce(achs);
  };
  live(el, paint);
  const off = on("impact", () => el.isConnected ? paint() : off());
  refreshImpact();
  return el;
}

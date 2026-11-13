import { api, streamChat } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { state, on, prefs, setPref, refreshConversations, refreshPlugins, refreshLibrary, refreshMesh, modelName, isSwarm, baseId, swarmOf } from "./state.js";
import { openPath, swarmBadge, layerStrip, hopColor, layersText } from "./path.js";
import { dots, serveBlocker } from "./models.js";
import { h, icon, brandMark, nodeAvatar, fmt, short, regionName, popover, closePopover, tooltip, copyText, toast, pluginIcon, modal } from "./ui.js";
import { nodeButton, routePopover } from "./nodepanel.js";
import { flag, meshField } from "./fx.js";

const SUGGESTIONS = [
  { ic: "globe", t: "Plan a 3-day trip to Lisbon", d: "with a food-first itinerary" },
  { ic: "spark", t: "Explain transformers like I'm 12", d: "then quiz me on it" },
  { ic: "code", t: "Write a script to rename photos by date", d: "in Python, with comments" },
  { ic: "calculator", t: "What's 18% of 2,450?", d: "exact math via the Calculator plugin", p: "What is 2450 * 18 / 100?" },
];
const DRAFTS = "mycel.drafts";
const drafts = () => JSON.parse(localStorage.getItem(DRAFTS) || "{}");
const saveDraft = (id, text) => { const d = drafts(); if (text) d[id] = text; else delete d[id]; localStorage.setItem(DRAFTS, JSON.stringify(d)); };
const MAX_TOOL_STEPS = 6;

function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? "Up late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
}
const apiMessage = (m) => {
  const out = { role: m.role, content: m.content || "" };
  if (m.tool_calls) out.tool_calls = m.tool_calls;
  if (m.role === "tool") out.tool_call_id = m.tool_call_id;
  return out;
};

export function render(container, convId, { go }) {
  let conv = null, busy = false, controller = null;
  const approvals = new Map();   // tool_call_id -> resolve(decision)
  const running = new Set();
  const offs = [];

  // ------------------------------------------------------------ chrome
  const sideToggle = h("button", { class: "icon-btn sb-toggle", "aria-label": "Toggle sidebar", title: "Toggle sidebar (⌘\\)",
    onclick: () => document.dispatchEvent(new CustomEvent("mycel:toggle-sidebar")) }, icon("sidebar"));
  const titleEl = h("button", { class: "chat-title", title: "Rename chat" });
  const newBtn = h("button", { class: "icon-btn", "aria-label": "New chat", title: "New chat", onclick: () => go("#/chat") }, icon("edit"));
  const topbar = h("div", { class: "topbar glass" }, sideToggle, titleEl, h("div", { class: "spacer" }), newBtn, nodeButton());
  const threadInner = h("div", { class: "thread-inner" });
  const jump = h("button", { class: "jump", "aria-label": "Jump to latest", onclick: () => thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" }) }, icon("down", "sm"));
  const thread = h("div", { class: "thread" }, threadInner);
  const ta = h("textarea", { rows: 1, placeholder: "Ask anything…", "aria-label": "Message" });
  const sendBtn = h("button", { class: "send", "aria-label": "Send", disabled: true }, icon("send"));
  const modelBtn = h("button", { class: "cchip", "aria-haspopup": "menu", title: "Choose a model" });
  const routeBtn = h("button", { class: "cchip route", "aria-haspopup": "dialog", title: "Where this message will run" });
  const toolsBtn = h("button", { class: "cchip", "aria-haspopup": "menu", title: "Plugins" });
  const composer = h("div", { class: "composer-wrap" },
    h("div", { class: "composer" }, ta,
      h("div", { class: "row" }, modelBtn, routeBtn, toolsBtn, h("div", { class: "spacer" }), sendBtn)),
    h("div", { class: "foot-note" }, "Every reply is signed by the GPU that wrote it."));
  container.append(h("div", { class: "chat" }, topbar, h("div", { class: "thread-wrap" }, thread, jump), composer));

  // ------------------------------------------------------------ header & composer widgets
  function currentModel() { return conv?.model || prefs.model || "auto"; }
  const localModels = () => (state.status?.serving ? state.status.backend.models : []);
  const where = (id) => (localModels().includes(id) ? "your GPU" : "the mesh");
  function resolvedModel() {
    const m = currentModel();
    if (m !== "auto") return m;
    return prefs.preferLocal && localModels().length ? localModels()[0] : state.auto;
  }
  // Where will the next message run? Mirrors the node's routing rules.
  function destination() {
    const m = resolvedModel();
    if (!m) return { key: "none", label: "No model online" };
    const sw = swarmOf(m);
    if (isSwarm(m) || (sw?.complete && !state.models.find((x) => x.id === baseId(m))?.available)) {
      return { key: "swarm", label: sw?.hops ? `Swarm · ${sw.hops} GPUs` : "Swarm" };
    }
    const local = localModels().includes(m);
    if (local && prefs.preferLocal) return { key: "local", label: "Your GPU · private" };
    if (local) return { key: "mesh", label: "Fastest node" };
    return { key: "mesh", label: "Via the mesh" };
  }
  function drawHeader() {
    const m = currentModel(), r = resolvedModel();
    modelBtn.replaceChildren(icon(isSwarm(m) ? "layers" : "spark", "sm"), h("span", {}, m === "auto" ? (r ? `Auto · ${modelName(r)}` : "Auto") : modelName(m)), icon("down", "sm"));
    modelBtn.classList.toggle("swarm", isSwarm(m) || (m === "auto" && isSwarm(r)));
    const d = destination();
    routeBtn.dataset.dest = d.key;
    routeBtn.replaceChildren(icon(d.key === "local" ? "lock" : d.key === "swarm" ? "layers" : "globe", "sm"), h("span", {}, d.label));
    routeBtn.title = d.key === "swarm" ? "Split across several members' GPUs — your text stays on this device" : "Where this message will run";
    const n = state.tools.filter((t) => t.spec).length;
    toolsBtn.replaceChildren(icon("puzzle", "sm"), h("span", {}, n ? `${n}` : "Tools"));
    toolsBtn.title = `${n} plugin tools available`;
    titleEl.textContent = conv?.title || "New chat";
    titleEl.disabled = !conv;
  }
  routeBtn.addEventListener("click", () => (destination().key === "swarm" ? explainSwarm(resolvedModel()) : routePopover(routeBtn)));
  function explainSwarm(id) {
    const sw = swarmOf(id) || {};
    modal({ title: "Swarm inference", iconEl: h("span", { class: "ic-tile swarm" }, icon("layers")),
      subtitle: `${modelName(baseId(id))} is too big for most single GPUs, so ${sw.hosts || "several"} members each hold a slice of its ${sw.layers || ""} layers.`,
      body: h("div", { class: "explain" },
        sw.coverage ? [layerStrip({ layers: sw.layers, coverage: sw.coverage, height: 16 }),
          h("div", { class: "strip-axis" }, h("span", {}, "Layer 1"), h("span", {}, sw.min_replicas > 1 ? `Every layer on ≥ ${sw.min_replicas} members` : "Some layers on a single member — another host would add a spare"), h("span", {}, `Layer ${sw.layers}`))] : null,
        h("div", { class: "perm" }, icon("route", "sm"), h("span", {}, h("b", {}, "Your node plans the route. "), `It picks the fastest chain of members that covers every layer — right now ${sw.hops || "–"} hops, about ${sw.est_tps ? fmt(sw.est_tps, 1) : "–"} tokens/s.`)),
        h("div", { class: "perm" }, icon("eyeOff", "sm"), h("span", {}, h("b", {}, "Your words stay here. "), "This device turns your text into numbers and back. Members in the chain only ever see the numbers passing through their layers.")),
        h("div", { class: "perm" }, icon("refresh", "sm"), h("span", {}, h("b", {}, "It heals itself. "), "If a member drops out mid-reply, your node re-routes their layers to a replica and carries on.")),
        h("div", { class: "perm" }, icon("shieldCheck", "sm"), h("span", {}, h("b", {}, "It checks the math. "), "One slice of every reply is recomputed by an independent member; a node that lies is caught and dropped.")),
        h("div", { class: "hint" }, "Slower than one big GPU, but it runs models no single member could.")),
      actions: [{ label: "Got it", kind: "primary" }] });
  }
  titleEl.addEventListener("click", () => {
    if (!conv) return;
    const input = h("input", { class: "chat-title-input", value: conv.title, "aria-label": "Chat title" });
    const done = async (commit) => {
      if (commit && input.value.trim() && input.value.trim() !== conv.title) { conv.title = input.value.trim().slice(0, 120); await save(); }
      input.replaceWith(titleEl); drawHeader();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); });
    input.addEventListener("blur", () => done(true));
    titleEl.replaceWith(input); input.focus(); input.select();
  });
  modelBtn.addEventListener("click", async () => {
    if (!state.library) await refreshLibrary();
    const lib = state.library;
    const meta = (id) => lib?.models.find((x) => x.id === id);
    const pick = async (id) => { closePopover(); setPref("model", id); if (conv) { conv.model = id; await save(); } drawHeader(); };
    const row = (id, title, sub, right, { disabled = false, onClick } = {}) => h("button", {
      class: `opt ${disabled ? "disabled" : ""}`, role: "menuitemradio", "aria-checked": String(currentModel() === id), "aria-disabled": String(disabled),
      onclick: disabled ? (onClick || (() => {})) : () => pick(id) },
    h("div", { class: "pick-row" }, h("span", { class: "main-t" }, title), h("span", { class: "right" }, right, currentModel() === id ? icon("check", "sm") : null),
      h("span", { class: "meta" }, sub)));
    const local = state.status?.serving ? state.status.backend.models : [];
    const onMesh = state.models.filter((m) => m.nodes > 0 && (!local.includes(m.id) || m.nodes > 1));
    const r = resolvedModel();
    const autoSub = prefs.preferLocal && localModels().length
      ? `Private first: your GPU's ${modelName(localModels()[0])} answers. Switch to Fastest in your node panel to use bigger mesh models.`
      : r ? `The most capable model with free capacity — now ${modelName(r)} on ${where(r)}` : "The most capable model with free capacity";
    const items = [row("auto", "Auto", autoSub, h("span", { class: "badge grad" }, "Recommended"))];
    if (local.length) {
      items.push(h("hr"), h("div", { class: "label" }, "On this device · private"));
      for (const id of local) {
        const m = meta(id);
        items.push(row(id, modelName(id), `Runs on your GPU — prompts never leave this device${m?.est_tokens_per_s ? ` · ~${fmt(m.est_tokens_per_s)} tok/s` : ""}`,
          h("span", {}, m ? dots(m.capability) : null)));
      }
    }
    items.push(h("hr"), h("div", { class: "label" }, "On the mesh"));
    const meshRows = onMesh.filter((m) => !local.includes(m.id) || m.nodes > 1);
    for (const m of meshRows) {
      const others = m.nodes - (local.includes(m.id) ? 1 : 0);
      items.push(row(m.id, m.name || m.id, `${others} member${others === 1 ? "" : "s"} · up to ~${fmt(m.tps, 0)} tok/s · needs ${m.memory_gb ? fmt(m.memory_gb, 0) + " GB" : "–"} to serve`,
        h("span", {}, m.capability ? dots(m.capability) : null,
          h("span", { class: "badge" }, h("span", { class: "dot", style: { background: m.available ? "var(--good)" : "var(--warn)" } }), m.available ? "Ready" : "Busy"))));
    }
    if (!meshRows.length) items.push(h("div", { class: "opt disabled" }, h("span", { class: "sub-t" }, "Nobody else is sharing a model yet — you're early.")));
    const swarms = state.models.filter((m) => m.swarm);
    if (swarms.length) {
      items.push(h("hr"), h("div", { class: "label swarm-label" }, icon("layers", "sm"), "Swarm · one model, many GPUs"),
        h("div", { class: "swarm-note" }, "Too big for any one member, so it runs layer by layer across several. Your words never leave this device."));
      for (const m of swarms) {
        const sw = m.swarm, id = `${m.id}@swarm`;
        if (sw.complete) {
          items.push(row(id, m.name || m.id, `Split across ${sw.hops} GPUs · ~${fmt(sw.est_tps, 1)} tok/s · ${sw.hosts} hosts${sw.min_replicas > 1 ? `, every layer ×${sw.min_replicas}` : ""}`,
            h("span", {}, m.capability ? dots(m.capability) : null, swarmBadge("Swarm"))));
        } else {
          items.push(row(id, m.name || m.id, `${Math.round(sw.covered * 100)}% assembled — needs ~${fmt(sw.need_gb, 0)} GB more. Host a slice to help finish it.`,
            h("span", {}, swarmBadge(`${Math.round(sw.covered * 100)}%`, "dim")), { disabled: true, onClick: () => { closePopover(); go("#/models"); } }));
        }
      }
    }
    const missing = (lib?.models || []).filter((m) => !state.models.some((x) => x.id === m.id) && !m.id.startsWith("tiny-")).slice(0, 5);
    if (missing.length) {
      items.push(h("hr"), h("div", { class: "label" }, "Not available yet"));
      for (const m of missing) {
        const block = serveBlocker(m, lib);
        items.push(row(m.id, m.name, block ? `${block} — nobody is sharing it yet` : `Nobody is sharing it yet — your device can (${fmt(m.size_gb, 1)} GB)`,
          h("span", {}, dots(m.capability), !block ? h("span", { class: "badge grad" }, "Serve it") : null),
          { disabled: true, onClick: !block ? () => { closePopover(); go("#/models"); } : undefined }));
      }
    }
    popover(modelBtn, items, { cls: "models", above: true });
  });
  toolsBtn.addEventListener("click", () => {
    const tools = state.tools.filter((t) => t.spec);
    const byPlugin = new Map();
    for (const t of tools) byPlugin.set(t.plugin, t);
    const rows = [...byPlugin.values()].map((t) => {
      const p = state.plugins.find((x) => x.id === t.plugin) || { category: "Custom", icon: t.icon };
      return h("div", { class: "opt" }, pluginIcon(p), h("div", {}, h("div", { class: "main-t" }, t.plugin_name),
        h("div", { class: "sub-t" }, t.permission === "auto" ? "Runs automatically" : "Asks before each use")));
    });
    popover(toolsBtn, [h("div", { class: "label" }, "Enabled plugins"), ...rows,
      h("hr"), h("button", { class: "opt", onclick: () => { closePopover(); go("#/plugins"); } }, icon("puzzle", "sm"), h("span", { class: "main-t" }, "Browse the marketplace"))], { above: true });
  });

  // ------------------------------------------------------------ messages
  function nearBottom() { return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140; }
  thread.addEventListener("scroll", () => jump.classList.toggle("show", !nearBottom()), { passive: true });
  function scrollDown(force) { if (force || nearBottom()) thread.scrollTop = thread.scrollHeight; }

  let meshLine = null, emptyHadModels = null;
  function meshLineText(s) {
    if (state.membership && !state.membership.member) return "Takes about a second · no account needed";
    if (s.active_nodes <= 1) return "You're the first member online · the mesh grows with every GPU that joins";
    return `${s.active_nodes} members across ${s.regions || 1} region${s.regions === 1 ? "" : "s"} · ${fmt(s.accel_memory_gb)} GB of GPU memory · ${Object.keys(s.models).length} models`;
  }
  function empty() {
    const s = state.stats;
    emptyHadModels = `${!!state.models.length}|${state.membership?.member}`;
    const cards = SUGGESTIONS.map((sg) => h("button", { onclick: () => { ta.value = sg.p || `${sg.t} ${sg.d}`; send(); } },
      h("span", { class: "ic" }, icon(sg.ic)), h("span", {}, h("div", { class: "t" }, sg.t), h("div", { class: "d" }, sg.d))));
    const notMember = state.membership && !state.membership.member;
    const noModels = state.online && !state.models.length;
    return h("div", { class: "hero" }, meshField({ density: 0.00004 }),
      h("div", { class: "hero-mark" }, brandMark(56, "hero")),
      h("h1", {}, `${greeting()}.`, h("span", { class: "g", style: { display: "block" } }, "What should we explore?")),
      h("p", {}, notMember ? "Join the global mesh to chat with models running on members' GPUs around the world."
        : noModels ? (state.stats?.active_nodes <= 1
          ? "You're early — the mesh is just you right now. Serve a model on your GPU to start chatting, and you'll be the first thing the next member can use."
          : "Nobody is sharing a model at this moment. Serve one on your GPU to start chatting.")
        : "Answered by people's GPUs around the world — every reply signed by the machine that wrote it."),
      notMember ? h("button", { class: "btn glow lg", onclick: async (e) => {
          e.currentTarget.disabled = true;
          e.currentTarget.replaceChildren(h("span", { class: "spin" }), "Creating your node identity…");
          try { await api.post("/api/membership/join-global", {}); await refreshMesh(); toast("Welcome to the mesh"); }
          catch (err) { toast(err.message, "bad"); }
          draw(); } }, icon("globe"), "Join the global mesh")
        : noModels ? h("button", { class: "btn glow lg", onclick: () => go("#/models") }, icon("bolt"), "Pick a model to serve")
        : [swarmBanner(), h("div", { class: "suggest" }, cards)],
      meshLine = h("div", { class: "mesh-line" }, h("span", { class: "pulse" }), h("span", {}, s ? meshLineText(s) : "")));
  }
  function swarmBanner() {
    const sw = state.models.filter((m) => m.swarm?.complete).sort((a, b) => b.capability - a.capability)[0];
    if (!sw) return null;
    return h("button", { class: "swarm-banner", onclick: async () => { setPref("model", `${sw.id}@swarm`); if (conv) { conv.model = `${sw.id}@swarm`; await save(); } drawHeader(); ta.focus(); toast(`${sw.name} · Swarm selected`); } },
      h("span", { class: "sb-ic" }, icon("layers")),
      h("span", { class: "sb-txt" }, h("b", {}, `${sw.name} is live as a swarm`),
        h("span", {}, `Split across ${sw.swarm.hops} members' GPUs right now — a model no single machine here could run. Your words stay on this device.`)),
      h("span", { class: "sb-go" }, "Try it", icon("arrowRight", "sm")));
  }
  function provenance(meta) {
    if (meta.mode === "swarm" && meta.path) return swarmProvenance(meta);
    const chip = h("span", { class: "prov", tabindex: 0, role: "button", title: "See the path this reply took", onclick: () => openPath(meta),
      onkeydown: (e) => { if (e.key === "Enter") openPath(meta); } });
    const region = regionName(meta.location);
    chip.append(...[nodeAvatar(meta.served_by || "", 18),
      meta.local ? "Your device" : `Node ${short(meta.served_by)}`,
      meta.tier ? h("span", { class: "sep" }) : null, meta.tier || null,
      region ? h("span", { class: "sep" }) : null, region || null,
      meta.tokens_per_s ? h("span", { class: "sep" }) : null, meta.tokens_per_s ? `${fmt(meta.tokens_per_s, 0)} tok/s` : null,
      meta.receipt_verified ? h("span", { class: "sep" }) : null,
      meta.receipt_verified ? h("span", { class: "ver" }, icon("shieldCheck", "sm"), "Verified") : null].filter(Boolean));
    tooltip(chip, () => h("div", {},
      h("div", { style: { fontWeight: 600, marginBottom: "4px" } }, meta.local ? "Answered on this device" : "Answered by a mesh volunteer"),
      h("div", { class: "dim" }, `Model: ${meta.model || "–"}`),
      meta.device ? h("div", { class: "dim" }, `Hardware: ${meta.device}${meta.simulated ? " (simulated)" : ""}`) : null,
      region ? h("div", { class: "dim" }, `Region: ${region} (coarse, opt-in)`) : null,
      meta.latency_ms != null ? h("div", { class: "dim" }, `First token: ${fmt(meta.latency_ms)} ms · ${fmt(meta.tokens)} tokens`) : null,
      meta.attempts > 1 ? h("div", { class: "dim" }, `Rerouted ${meta.attempts - 1}× before succeeding`) : null,
      meta.receipt_verified ? h("div", { style: { marginTop: "6px", color: "var(--good)" } },
        meta.local ? "Generated locally — your prompt never left this device." : "Signed receipt checked: this exact text came from that node.") : null,
      h("div", { class: "muted", style: { marginTop: "6px" } }, "Click to see the full path")));
    return chip;
  }
  function swarmProvenance(meta) {
    const hops = meta.path.hops;
    const ev = meta.path.events || [];
    return h("button", { class: "prov swarm", title: "See the path this reply took", onclick: () => openPath(meta) },
      h("span", { class: "avstack" }, hops.slice(0, 5).map((hp) => nodeAvatar(hp.node, 18))),
      h("span", { class: "sw-t" }, `Swarm · ${hops.length} GPUs`),
      meta.tokens_per_s ? [h("span", { class: "sep" }), `${fmt(meta.tokens_per_s, 1)} tok/s`] : null,
      ev.some((e) => e.kind === "failover") ? [h("span", { class: "sep" }), h("span", { class: "heal" }, icon("refresh", "sm"), "healed")] : null,
      h("span", { class: "sep" }), h("span", { class: "ver" }, icon("eyeOff", "sm"), "text stayed here"),
      h("span", { class: "sep" }), h("span", { class: "path-l" }, icon("route", "sm"), "Path"));
  }
  // Live view of a swarm reply assembling: the layer bar fills as each stage finishes the prompt.
  function swarmLive(live) {
    const segs = live.hops.map((hp, i) => ({ a: hp.layers[0], b: hp.layers[1], color: hopColor(i), pending: i >= live.done }));
    const cur = live.hops[Math.min(live.done, live.hops.length - 1)];
    const text = live.healing ? `Re-routing layers ${live.healing[0] + 1}–${live.healing[1]} around a member who dropped out…`
      : live.done < live.hops.length ? `Routing your prompt through ${live.hops.length} GPUs · ${layersText(cur.layers).toLowerCase()} on node ${short(cur.node)}…`
      : `All ${live.layers} layers ready · generating`;
    return h("div", { class: "swarm-live" }, h("div", { class: "sl-top" }, h("span", { class: "spin" }), h("span", {}, text)),
      layerStrip({ layers: live.layers, segments: segs, height: 8, live: true }));
  }

  function toolCard(call, result) {
    const tool = state.tools.find((t) => t.spec?.function.name === call.function.name);
    const p = state.plugins.find((x) => x.id === tool?.plugin) || { category: "Custom", icon: "puzzle" };
    let args = call.function.arguments;
    try { args = JSON.stringify(JSON.parse(args || "{}"), null, 2); } catch { /* keep raw */ }
    const pending = approvals.has(call.id), isRunning = running.has(call.id);
    const st = pending ? h("span", { class: "st" }, "Needs your OK")
      : isRunning ? h("span", { class: "st" }, h("span", { class: "spin" }), "Running")
      : result ? h("span", { class: "st", style: { color: result.denied ? "var(--text-3)" : result.ok === false ? "var(--bad)" : "var(--good)" } },
        icon(result.denied ? "x" : result.ok === false ? "alert" : "check", "sm"), result.denied ? "Declined" : result.ok === false ? "Failed" : "Done")
      : h("span", { class: "st" }, "Waiting");
    const card = h("div", { class: "tool" },
      h("div", { class: "tool-head" }, h("span", { class: "ic" }, icon(p.icon || "puzzle", "sm")),
        h("span", {}, h("span", { class: "name" }, tool?.plugin_name || call.function.name), h("span", { class: "muted" }, ` · ${tool?.tool || "tool"}`)), st));
    const body = h("div", { class: "tool-body" }, args === "{}" ? "" : args);
    if (args && args !== "{}") card.appendChild(body);
    if (result && !result.denied) card.appendChild(h("div", { class: "tool-body", style: { color: "var(--text)" } }, result.content));
    if (pending) {
      const decide = (d) => { const r = approvals.get(call.id); approvals.delete(call.id); r && r(d); };
      card.appendChild(h("div", { class: "tool-approve" },
        h("div", { class: "q" }, `The assistant wants to use ${tool?.plugin_name || "a plugin"} with the input above. It runs on your device.`),
        h("button", { class: "btn primary sm", onclick: () => decide("once") }, "Allow once"),
        h("button", { class: "btn sm", onclick: () => decide("always") }, "Always allow"),
        h("button", { class: "btn ghost sm", onclick: () => decide("deny") }, "Deny")));
    }
    return card;
  }

  function messageEl(m, idx, isLast) {
    if (m.role === "user") {
      return h("div", { class: "msg user" }, h("div", { class: "uwrap" },
        h("div", { class: "bubble" }, m.content),
        h("div", { class: "msg-actions", style: { justifyContent: "flex-end" } },
          h("button", { class: "icon-btn sm", title: "Copy", "aria-label": "Copy", onclick: () => copyText(m.content) }, icon("copy", "sm")),
          h("button", { class: "icon-btn sm", title: "Edit", "aria-label": "Edit and resend", onclick: () => editFrom(idx) }, icon("edit", "sm")))));
    }
    if (m.role === "tool") return null;
    const el = h("div", { class: `msg assistant ${isLast ? "last" : ""}` });
    if (m.pending && m.live && !m.content) el.appendChild(swarmLive(m.live));
    else if (m.pending && !m.content && !m.tool_calls && !m.reasoning) {
      el.appendChild(h("div", { class: "routing" }, h("span", { class: "spin" }), "Finding the best node on the mesh…"));
    }
    if (m.reasoning) {
      const thinking = m.pending && !m.content;
      const det = h("details", { class: "think" },
        h("summary", {}, thinking ? h("span", { class: "spin" }) : icon("spark", "sm"), thinking ? "Thinking…" : "Thought process"),
        h("div", { class: "think-body" }, m.reasoning.trim()));
      if (thinking) det.open = true;
      el.appendChild(det);
    }
    if (m.content) {
      const body = h("div", { class: "body md" });
      body.innerHTML = renderMarkdown(m.content || "", { streaming: !!m.pending });  // safe: renderer escapes all input
      el.appendChild(body);
    }
    for (const call of m.tool_calls || []) {
      const res = conv.messages.find((x) => x.role === "tool" && x.tool_call_id === call.id);
      el.appendChild(toolCard(call, res));
    }
    if (m.error) el.appendChild(h("div", { class: "warn-box" }, icon("alert"), h("div", { style: { flex: 1 } }, m.error),
      isLast ? h("button", { class: "btn sm", onclick: regenerate }, icon("refresh", "sm"), "Retry") : null));
    if (m.stopped) el.appendChild(h("div", { class: "muted", style: { fontSize: "12.5px" } }, "Stopped"));
    if (!m.pending && (m.content || m.error)) {
      el.appendChild(h("div", { class: "msg-actions" },
        h("button", { class: "icon-btn sm", title: "Copy", "aria-label": "Copy", onclick: () => copyText(m.content) }, icon("copy", "sm")),
        isLast ? h("button", { class: "icon-btn sm", title: "Regenerate", "aria-label": "Regenerate", onclick: regenerate }, icon("refresh", "sm")) : null,
        m.meta && m.meta.served_by ? provenance(m.meta) : null));
    }
    return el;
  }

  // Keyed rendering: a message's DOM is rebuilt only when what it shows changes, so streaming
  // one reply doesn't re-create (and re-animate) the whole thread.
  let raf = 0;
  const cache = new WeakMap();  // message -> { sig, el }
  function signature(m, isLast) {
    const calls = (m.tool_calls || []).map((c) => [c.id, approvals.has(c.id), running.has(c.id),
      conv.messages.some((x) => x.role === "tool" && x.tool_call_id === c.id)]);
    return JSON.stringify([m.content?.length, m.reasoning?.length, !!m.pending, m.error, m.stopped, !!m.meta, calls, isLast,
      m.live ? [m.live.done, !!m.live.healing] : null]);
  }
  function draw() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      const stick = nearBottom();
      if (!conv || !conv.messages.length) { threadInner.replaceChildren(empty()); return; }
      if (threadInner.querySelector(".hero")) threadInner.replaceChildren();
      const visible = conv.messages.map((m, i) => [m, i]).filter(([m]) => m.role !== "tool");
      const lastIdx = visible.length ? visible[visible.length - 1][1] : -1;
      const els = [];
      for (const [m, i] of visible) {
        const isLast = i === lastIdx;
        const sig = signature(m, isLast);
        const hit = cache.get(m);
        if (hit && hit.sig === sig) { els.push(hit.el); continue; }
        const el = messageEl(m, i, isLast);
        if (!el) continue;
        if (hit) el.classList.add("still");  // already on screen: update in place, no entrance animation
        cache.set(m, { sig, el });
        els.push(el);
      }
      const current = [...threadInner.children];
      if (current.length !== els.length || current.some((c, k) => c !== els[k])) threadInner.replaceChildren(...els);
      if (stick) scrollDown(true);
    });
  }
  thread.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) copyText(b.closest(".codeblock").querySelector("pre").textContent, "Code copied");
  });

  // ------------------------------------------------------------ conversation flow
  async function load(id) {
    ta.value = drafts()[id || "new"] || "";
    setTimeout(autosize);
    if (!id) { conv = null; draw(); drawHeader(); return; }
    try { conv = await api.get(`/api/conversations/${id}`); }
    catch { toast("That chat no longer exists", "bad"); go("#/chat"); return; }
    draw(); drawHeader();
    setTimeout(() => scrollDown(true), 30);
  }
  async function save() {
    if (!conv) return;
    const clean = conv.messages.map(({ pending, live, ...rest }) => rest);
    try { await api.put(`/api/conversations/${conv.id}`, { messages: clean, model: conv.model, title: conv.title }); }
    catch (e) { toast(e.message, "bad"); }
    refreshConversations();
  }
  function setBusy(b) {
    busy = b;
    sendBtn.classList.toggle("stop", b);
    sendBtn.replaceChildren(icon(b ? "stop" : "send"));
    sendBtn.setAttribute("aria-label", b ? "Stop" : "Send");
    sendBtn.disabled = !b && !ta.value.trim();
  }
  async function send() {
    const text = ta.value.trim();
    if (!text || busy) return;
    if (!conv) {
      conv = await api.post("/api/conversations", { title: text.slice(0, 60), model: currentModel() });
      history.replaceState(null, "", `#/chat/${conv.id}`);
    }
    ta.value = ""; autosize();
    saveDraft("new", ""); saveDraft(conv.id, "");
    drawHeader();
    conv.messages.push({ role: "user", content: text, at: Date.now() / 1000 });
    draw(); scrollDown(true);
    await runTurn();
  }
  async function askApproval(call) {
    const p = new Promise((resolve) => approvals.set(call.id, resolve));
    draw();
    return p;
  }
  async function runTurn() {
    setBusy(true);
    controller = new AbortController();
    try {
      for (let step = 0; step < MAX_TOOL_STEPS; step++) {
        const tools = state.tools.filter((t) => t.spec);
        const msg = { role: "assistant", content: "", pending: true, at: Date.now() / 1000 };
        conv.messages.push(msg);
        draw();
        try {
          await streamChat({
            model: currentModel(), messages: conv.messages.filter((m) => m !== msg && !m.error).map(apiMessage),
            tools: tools.map((t) => t.spec), max_tokens: prefs.maxTokens, mesh: { prefer_local: prefs.preferLocal },
          }, (ev) => {
            if (ev.progress) { onProgress(msg, ev); draw(); }
            else if (ev.delta) { msg.content += ev.delta; draw(); }
            else if (ev.reasoning) { msg.reasoning = (msg.reasoning || "") + ev.reasoning; draw(); }
            else if (ev.tool_calls) { msg.tool_calls = ev.tool_calls; draw(); }
            else if (ev.done) { msg.meta = ev; if (ev.error) msg.error = ev.error; if (ev.mode === "swarm" && !ev.error) flag("swarm-reply"); }
          }, controller.signal);
        } catch (e) {
          if (e.name === "AbortError") msg.stopped = true;
          else msg.error = e.message;
        }
        delete msg.pending;
        draw();
        if (!msg.tool_calls?.length || msg.stopped || msg.error) break;
        for (const call of msg.tool_calls) {
          const tool = tools.find((t) => t.spec.function.name === call.function.name);
          if (!tool) {
            conv.messages.push({ role: "tool", tool_call_id: call.id, content: "Error: that tool isn't available.", ok: false });
            continue;
          }
          let decision = tool.permission === "auto" ? "auto" : await askApproval(call);
          if (controller.signal.aborted) decision = "deny";
          if (decision === "always") {
            await api.post("/api/plugins/update", { id: tool.plugin, permission: "auto" });
            refreshPlugins();
          }
          if (decision === "deny") {
            conv.messages.push({ role: "tool", tool_call_id: call.id, content: "The user declined to run this tool.", ok: false, denied: true });
            draw();
            continue;
          }
          running.add(call.id); draw();
          try {
            const r = await api.post("/api/plugins/call", { name: call.function.name, arguments: call.function.arguments, approved: true });
            conv.messages.push({ role: "tool", tool_call_id: call.id, content: r.result, ok: r.ok });
          } catch (e) {
            conv.messages.push({ role: "tool", tool_call_id: call.id, content: `Error: ${e.message}`, ok: false });
          }
          running.delete(call.id); draw();
        }
        if (controller.signal.aborted) break;
      }
    } finally {
      for (const r of approvals.values()) r("deny");
      approvals.clear();
      setBusy(false);
      draw();
      await save();
    }
  }
  function onProgress(msg, ev) {
    if (ev.progress === "route") msg.live = { hops: ev.hops, layers: ev.layers, done: 0 };
    else if (!msg.live) return;
    else if (ev.progress === "prefill") { msg.live.done = ev.hop + 1; msg.live.healing = null; }
    else if (ev.progress === "failover") msg.live.healing = ev.layers;
  }
  async function regenerate() {
    if (busy || !conv) return;
    let i = conv.messages.length - 1;
    while (i >= 0 && conv.messages[i].role !== "user") i--;
    conv.messages = conv.messages.slice(0, i + 1);
    draw();
    await runTurn();
  }
  function editFrom(idx) {
    if (busy) return;
    ta.value = conv.messages[idx].content;
    conv.messages = conv.messages.slice(0, idx);
    draw(); autosize(); ta.focus();
    setBusy(false);
  }

  // ------------------------------------------------------------ composer
  function autosize() { ta.style.height = "auto"; ta.style.height = Math.min(240, ta.scrollHeight) + "px"; sendBtn.disabled = !busy && !ta.value.trim(); }
  ta.addEventListener("input", () => { autosize(); saveDraft(conv?.id || "new", ta.value); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    if (e.key === "Escape" && busy) controller?.abort();
  });
  sendBtn.addEventListener("click", () => (busy ? controller?.abort() : send()));

  offs.push(on("mesh", () => {
    drawHeader();
    if (conv && conv.messages.length) return;
    if (emptyHadModels !== `${!!state.models.length}|${state.membership?.member}`) draw();  // availability changed: rebuild
    else if (meshLine && state.stats) meshLine.lastChild.textContent = meshLineText(state.stats);
  }));
  offs.push(on("plugins", drawHeader));
  offs.push(on("prefs", drawHeader));
  drawHeader();
  load(convId);
  setTimeout(() => ta.focus(), 50);
  return () => { offs.forEach((f) => f()); controller?.abort(); };
}

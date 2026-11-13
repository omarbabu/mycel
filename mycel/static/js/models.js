import { api } from "./api.js";
import { state, on, refreshLibrary, refreshMesh } from "./state.js";
import { h, icon, fmt, modal, toast, short } from "./ui.js";
import { modules } from "./cockpit.js";

export function dots(n) {
  return h("span", { class: "dots", title: `Capability ${n}/5 (relative guide)`, "aria-label": `Capability ${n} of 5` },
    [1, 2, 3, 4, 5].map((i) => h("i", { class: i <= n ? "on" : "" })));
}

// Why can't this device serve a model? (null = it can)
export function serveBlocker(m, lib) {
  if (!lib.device.can_serve_hardware) return "This device has no supported GPU";
  if (!m.fits_device) return `Needs ${fmt(m.memory_gb, 0)} GB · you have ${fmt(lib.device.accel_memory_gb, 0)} GB`;
  if (m.est_tokens_per_s != null && m.est_tokens_per_s < 8) return `Too slow here (~${fmt(m.est_tokens_per_s)} tok/s)`;
  return null;
}

export function confirmServe(m, lib) {
  const start = (share) => async () => {
    try { await api.post("/api/library/serve", { id: m.id, share }); toast(`${m.downloaded ? "Loading" : "Downloading"} ${m.name} — ${share ? "it'll be shared with the mesh" : "private, just for you"}`); refreshLibrary(); refreshMesh(); }
    catch (e) { toast(e.message, "bad"); }
  };
  modal({ title: `Run ${m.name} on your GPU`, subtitle: m.blurb,
    body: h("div", {},
      h("div", { class: "kv" },
        h("dt", {}, "Download"), h("dd", {}, m.downloaded ? "Already on this device" : `${fmt(m.size_gb, 1)} GB from Hugging Face (${m.hf.split(":")[0]})`),
        h("dt", {}, "Memory"), h("dd", {}, `${fmt(m.memory_gb, 0)} GB of your ${fmt(lib.device.accel_memory_gb, 0)} GB`),
        h("dt", {}, "Speed here"), h("dd", {}, m.est_tokens_per_s ? `~${fmt(m.est_tokens_per_s)} tokens/s (estimate)` : "Measured after it loads"),
        h("dt", {}, "License"), h("dd", {}, m.license)),
      h("div", { class: "choice-row" },
        h("div", { class: "choice" }, icon("lock"), h("b", {}, "Private"), h("span", {}, "Only you chat with it. The mesh never sees it — not even its name.")),
        h("div", { class: "choice lit" }, icon("globe"), h("b", {}, "Shared"), h("span", {}, "You chat with it, and members can use it within your limit. You can flip this any time.")))),
    actions: [{ label: "Cancel", kind: "ghost" }, { label: "Run privately", icon: "lock", onClick: start(false) },
      { label: m.downloaded ? "Run & share" : "Download, run & share", icon: "globe", kind: "glow", onClick: start(true) }] });
}

export function render(container) {
  const offs = [];
  const device = h("div");
  const rec = h("div");
  const list = h("div", { class: "card", style: { padding: "6px 0" } });
  const swarmBox = h("div", { class: "card mods-card", style: { marginBottom: "14px" } },
    h("div", { class: "mods-head" }, h("h2", {}, "How your GPU takes part"),
      h("p", {}, "Keep a model private, share it, host a slice of a giant — or all three. Your limit caps whatever you share.")),
    modules());
  container.append(h("div", { class: "view" }, h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Models"),
      h("p", {}, "Run a model on your GPU — privately or shared — or chat with anything the mesh runs."))),
    swarmBox, h("div", { class: "grid cols-2", style: { marginBottom: "14px" } }, device, rec), list)));

  function statusLine(lib) {
    const srv = lib.server;
    const m = lib.models.find((x) => x.serving_here) || lib.models.find((x) => x.id === (srv.model_id || lib.serve_model));
    if (lib.serving && m) return [h("span", { class: "badge ok" }, icon("check", "sm"), "Active"), ` ${m.name} · `,
      state.status?.sharing ? h("span", { style: { color: "var(--good)" } }, "shared with the mesh") : h("span", { class: "dim" }, "private — only you")];
    if (srv.status === "downloading") return [h("span", { class: "spin" }), ` Downloading ${m?.name || ""}${srv.progress != null ? ` · ${Math.round(srv.progress * 100)}%` : ""}`];
    if (srv.status === "loading") return [h("span", { class: "spin" }), ` Loading ${m?.name || ""} into GPU memory…`];
    if (srv.status === "ready") return [h("span", { class: "spin" }), ` Measuring speed…`];
    if (srv.status === "error") return [h("span", { class: "badge", style: { color: "var(--bad)" } }, icon("alert", "sm"), "Couldn't start"), ` ${srv.error || ""}`];
    return [h("span", { class: "muted" }, "No model active")];
  }

  function draw() {
    const lib = state.library;
    if (!lib) return;
    const d = lib.device;
    const srv = lib.server;
    device.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Your device"),
      h("div", { class: "kv", style: { margin: "10px 0 14px" } },
        h("dt", {}, "GPU"), h("dd", {}, d.name || "–"), h("dt", {}, "Usable memory"), h("dd", {}, `${fmt(d.accel_memory_gb, 1)} GB`),
        h("dt", {}, "Bandwidth"), h("dd", {}, d.bandwidth_gbs ? `${fmt(d.bandwidth_gbs)} GB/s (${state.status?.hardware?.bandwidth_source === "measured" ? "measured while serving" : "typical for this chip"})` : "–")),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } }, statusLine(lib)),
      srv.status === "downloading" && srv.progress != null ? h("div", { class: "meter", style: { marginTop: "10px" } }, h("i", { style: { width: `${srv.progress * 100}%` } })) : null,
      !srv.llama_server ? h("div", { class: "warn-box" }, icon("alert"), h("div", {}, "Install llama.cpp to serve models (macOS: ", h("span", { class: "mono" }, "brew install llama.cpp"), ").")) : null,
      lib.serve_model ? h("div", { style: { marginTop: "12px" } }, h("button", { class: "btn sm", onclick: async () => { await api.post("/api/library/stop", {}); toast("Model stopped"); refreshLibrary(); refreshMesh(); } }, icon("stop", "sm"), "Stop model")) : null));
    const r = lib.models.find((m) => m.id === lib.recommended);
    rec.replaceChildren(r ? h("div", { class: "card", style: { background: "var(--grad-soft)" } },
      h("div", { class: "hint", style: { marginBottom: "6px" } }, "Recommended for your device"),
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } }, h("div", { style: { fontSize: "20px", fontWeight: 650, letterSpacing: "-0.02em" } }, r.name), dots(r.capability)),
      h("p", { class: "dim", style: { margin: "6px 0 14px" } }, r.blurb),
      h("div", { class: "hint", style: { marginBottom: "14px" } }, `${fmt(r.size_gb, 1)} GB download · ${fmt(r.memory_gb, 0)} GB memory · ~${fmt(r.est_tokens_per_s)} tok/s here${r.mesh_nodes ? ` · ${r.mesh_nodes} others serve it` : ""}`),
      r.serving_here ? h("span", { class: "badge ok" }, icon("check", "sm"), "Active on your GPU")
        : h("button", { class: "btn glow", onclick: () => confirmServe(r, lib) }, icon("bolt", "sm"), r.downloaded ? "Run it" : "Download & run"))
      : h("div", { class: "card" }, h("h2", {}, "Use the mesh"), h("p", {}, "This device can't serve models, but you can chat with anything the mesh offers.")));
    list.replaceChildren(h("div", { class: "scroll-x" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Model", "Capability", "Download", "Memory", "On your device", "On the mesh", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, lib.models.map((m) => {
        const block = serveBlocker(m, lib);
        const busy = srv.model_id === m.id && ["downloading", "loading", "ready"].includes(srv.status);
        const action = m.serving_here ? h("span", { class: "badge ok" }, icon("check", "sm"), state.status?.sharing ? "Active · shared" : "Active · private")
          : busy ? h("span", { class: "badge" }, h("span", { class: "spin" }), srv.status === "downloading" ? `${Math.round((srv.progress || 0) * 100)}%` : "Loading")
          : block ? null : h("button", { class: "btn sm", onclick: () => confirmServe(m, lib) }, m.downloaded ? "Run" : "Get");
        return h("tr", { class: m.serving_here ? "me" : "" },
          h("td", {}, h("div", { style: { fontWeight: 600 } }, m.name, m.id === lib.recommended ? h("span", { class: "badge grad", style: { marginLeft: "8px" } }, "Best fit") : null),
            h("div", { class: "hint" }, `${m.family} · ${m.moe ? `${fmt(m.params_b, 0)}B MoE, ${fmt(m.active_b, 1)}B active` : `${fmt(m.params_b, 1)}B`} · ${m.license}`)),
          h("td", {}, dots(m.capability)),
          h("td", { class: "num" }, `${fmt(m.size_gb, 1)} GB`),
          h("td", { class: "num" }, `${fmt(m.memory_gb, 0)} GB`),
          h("td", {}, block ? h("span", { class: "muted" }, block) : h("span", {}, h("span", { style: { color: "var(--good)" } }, "✓ Fits"), m.est_tokens_per_s ? ` · ~${fmt(m.est_tokens_per_s)} tok/s` : "")),
          h("td", {}, !state.membership?.member ? h("span", { class: "muted" }, "Join to see")
            : m.mesh_nodes ? `${m.mesh_nodes} node${m.mesh_nodes === 1 ? "" : "s"}${m.mesh_tps ? ` · ~${fmt(m.mesh_tps)} tok/s` : ""}` : h("span", { class: "muted" }, "Nobody yet")),
          h("td", { class: "num" }, action));
      })))),
      h("div", { class: "hint", style: { padding: "10px 16px 6px" } }, "Capability is a coarse relative guide, not a benchmark. Speeds are estimates from your measured memory bandwidth; the real number is measured once a model loads."));
  }

  offs.push(on("library", draw));
  refreshLibrary();
  const t = setInterval(refreshLibrary, 1500);
  draw();
  return () => { clearInterval(t); offs.forEach((f) => f()); };
}

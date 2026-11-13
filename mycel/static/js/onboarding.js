import { api } from "./api.js";
import { state, setPref, refreshMesh } from "./state.js";
import { h, icon, brandMark, fmt, toast } from "./ui.js";
import { burst } from "./fx.js";
import { joinCard, presetPicker, locationEditor } from "./settings.js";
import { dots } from "./models.js";

export function showOnboarding() {
  let step = 0;
  const root = h("div", { class: "onboard", role: "dialog", "aria-modal": "true", "aria-label": "Welcome to Mycel" });
  document.body.appendChild(root);
  const done = () => { setPref("onboarded", true); root.remove(); };

  const steps = [welcome, membership, pickModel, contribute, privacy, ready];
  function dots() { return h("div", { class: "steps" }, steps.map((_, i) => h("i", { class: i <= step ? "on" : "" }))); }
  function next() { step++; draw(); }
  function nav(primary, { label = "Continue", disabled = false, skip = true } = {}) {
    return h("div", { class: "actions" },
      step > 0 ? h("button", { class: "btn ghost lg", onclick: () => { step--; draw(); } }, "Back") : null,
      primary === null ? null : primary || h("button", { class: "btn primary lg", disabled, onclick: next }, label),
      skip && step < steps.length - 1 ? h("button", { class: "btn ghost lg", onclick: done }, "Skip") : null);
  }

  function welcome() {
    return [h("div", { class: "logo" }, brandMark(72, "ob")),
      h("h1", {}, "Intelligence, shared."),
      h("p", { class: "lead" }, "Mycel is one global network of people sharing spare GPU time — so everyone gets a world-class AI assistant without renting a data center."),
      h("div", { class: "feature-list" },
        h("div", {}, icon("globe"), h("span", {}, h("b", {}, "One mesh, owned by no one. "), "No company in the middle. Every device that joins makes it faster for everyone.")),
        h("div", {}, icon("layers"), h("span", {}, h("b", {}, "Bigger than any one GPU. "), "Swarm models run layer by layer across many members' GPUs — and your words never leave your device.")),
        h("div", {}, icon("shieldCheck"), h("span", {}, h("b", {}, "Verified answers. "), "Every reply shows the exact path it took, signed or cross-checked along the way.")),
        h("div", {}, icon("sliders"), h("span", {}, h("b", {}, "You set the limit. "), "Share as much or as little of your GPU as you like.")),
        h("div", {}, icon("lock"), h("span", {}, h("b", {}, "Private by default. "), "End-to-end encrypted, chats stored only on your device, and nothing about you is shared unless you choose."))),
      nav(h("button", { class: "btn glow lg", onclick: next }, "Get started", icon("arrowRight", "sm")), { skip: false })];
  }

  let joining = false, showInvite = false;
  function membership() {
    const m = state.membership;
    const net = state.status?.network || {};
    if (m?.member && net.mode === "internet" && !net.online) {
      const known = (net.known || []).length;
      return [h("h1", {}, known ? "Finding the mesh…" : "You're ready — no relays yet."),
        h("p", { class: "lead" }, known
          ? "Your identity is set. Mycel is connecting to a relay so it can meet other members. This usually takes a few seconds."
          : "Your identity is set, but this build doesn't include a public relay yet. Add one a friend or your community runs, and you're on the mesh."),
        known ? h("div", { class: "spin", style: { margin: "8px auto 18px" } })
              : h("div", { class: "actions" }, h("a", { class: "btn glow", href: "#/settings/network" }, icon("relay", "sm"), "Add a relay")),
        nav()];
    }
    if (m?.member) {
      const s = state.stats;
      return [h("h1", {}, m.kind === "global" ? "You're on the mesh." : "You're in."),
        h("p", { class: "lead" }, m.kind === "global"
          ? `${s ? s.active_nodes : m.members_online} members are online, sharing ${s ? fmt(s.accel_memory_gb) + " GB of GPU memory" : "their GPUs"} across ${s?.regions || 1} regions.`
          : `You're a member of private mesh ${m.mesh_id.slice(0, 6)}, with ${m.members_online} members online.`),
        s ? h("div", { class: "tiles", style: { textAlign: "left" } },
          h("div", { class: "card tile" }, h("div", { class: "k" }, icon("users", "sm"), "Members"), h("div", { class: "v" }, fmt(s.active_nodes))),
          h("div", { class: "card tile" }, h("div", { class: "k" }, icon("spark", "sm"), "Capacity"), h("div", { class: "v" }, fmt(s.sustained_tokens_per_s), h("small", {}, "tok/s"))),
          h("div", { class: "card tile" }, h("div", { class: "k" }, icon("cpu", "sm"), "Models"), h("div", { class: "v" }, fmt(Object.keys(s.models).length)))) : null,
        nav()];
    }
    const joinBtn = h("button", { class: "btn glow lg", disabled: joining, onclick: async () => {
      joining = true; draw();
      try { await api.post("/api/membership/join-global", {}); await refreshMesh(); toast("Welcome to the mesh"); }
      catch (e) { toast(e.message, "bad"); }
      joining = false; draw();
    } }, joining ? [h("span", { class: "spin" }), "Creating your node identity…"] : [icon("globe", "sm"), "Join the global mesh"]);
    return [h("div", { class: "logo" }, brandMark(72, "ob3")), h("h1", {}, "Join the global mesh"),
      h("p", { class: "lead" }, "One tap. Your device makes a unique cryptographic identity (a second of work that keeps fake nodes out), then connects to members near you."),
      h("div", { class: "actions" }, joinBtn),
      h("div", { style: { marginTop: "22px" } }, showInvite
        ? h("div", { class: "panel" }, joinCard(m, () => { step++; draw(); }))
        : h("button", { class: "btn ghost sm", onclick: () => { showInvite = true; draw(); } }, "Have an invitation to a private mesh?")),
      nav(null, { label: "Continue", disabled: true })];
  }

  let lib = null, starting = false;
  function pickModel() {
    if (!lib) { api.get("/api/library").then((l) => { lib = l; draw(); }).catch(() => {}); return [h("div", { class: "spin" })]; }
    const r = lib.models.find((m) => m.id === lib.recommended);
    if (!r) {
      return [h("h1", {}, "Chat with the mesh"), h("p", { class: "lead" },
        "This device can't serve a model (it needs a supported GPU with 8 GB+), but you can chat with every model members share."), nav()];
    }
    const started = starting || lib.serve_model === r.id;
    return [h("h1", {}, "Your model"), h("p", { class: "lead" },
      "Pick the model your GPU runs. You'll chat with it privately on this device — and it's the same file every other member serving it runs. (You can also host a slice of a giant model later, in Models.)"),
      h("div", { class: "panel card", style: { background: "var(--grad-soft)" } },
        h("div", { class: "hint", style: { marginBottom: "6px" } }, `Best fit for your ${lib.device.name || "GPU"} (${fmt(lib.device.accel_memory_gb, 0)} GB)`),
        h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } }, h("div", { style: { fontSize: "22px", fontWeight: 660, letterSpacing: "-0.02em" } }, r.name), dots(r.capability)),
        h("p", { class: "dim", style: { margin: "6px 0 10px" } }, r.blurb),
        h("div", { class: "hint" }, `${fmt(r.size_gb, 1)} GB download · ${fmt(r.memory_gb, 0)} GB memory · ~${fmt(r.est_tokens_per_s)} tokens/s · ${r.license}`)),
      nav(started ? h("button", { class: "btn primary lg", onclick: next }, icon("check", "sm"), "Downloading — continue")
        : h("button", { class: "btn glow lg", onclick: async () => {
          starting = true; draw();
          try { await api.post("/api/library/serve", { id: r.id }); toast("Download started — it keeps going in the background"); }
          catch (e) { starting = false; toast(e.message, "bad"); draw(); return; }
          next();
        } }, icon("bolt", "sm"), r.downloaded ? `Serve ${r.name}` : `Download ${r.name}`), { skip: false }),
      started ? null : h("button", { class: "btn ghost sm", onclick: next }, "Not now — I'll just use the mesh"),
      h("div", { class: "hint", style: { marginTop: "14px" } }, "You can switch models any time in Models.")];
  }

  function contribute() {
    const st = state.status, srv = st?.model_server || {};
    const coming = st?.serving || ["downloading", "loading", "ready"].includes(srv.status);
    if (!coming) {
      return [h("h1", {}, "Use the mesh freely"), h("p", { class: "lead" },
        "You can chat with every model members run — including swarm giants. Later, run a model on your GPU or host a slice of one in Models."), nav()];
    }
    const box = h("div", { class: "panel" });
    const shared = () => !state.status.governor.limits.paused;
    const drawBox = () => box.replaceChildren(
      h("div", { class: "mode-seg big" }, [[false, "lock", "Keep it private", "Only you use it. The mesh never sees it."],
        [true, "globe", "Share it", "Members can use it within your limit."]].map(([v, ic, t, d]) =>
        h("button", { class: shared() === v ? "on" : "", onclick: async (e) => { await api.post("/api/limits", { paused: !v }); await refreshMesh(); if (v) burst(e.currentTarget); drawBox(); } },
          icon(ic), h("span", {}, h("b", {}, t), h("small", {}, d))))),
      shared() ? h("div", { style: { marginTop: "14px" } }, presetPicker(state.status.governor.limits.max_share, async (v) => {
        await api.post("/api/limits", { max_share: v }); await refreshMesh(); drawBox(); })) : null);
    drawBox();
    return [h("h1", {}, "Share it with the mesh?"),
      h("p", { class: "lead" }, "Your model is yours either way. Sharing lets members use it within a hard limit you set — your own chats never count. Flip it any time from the orb in the corner."),
      box, nav()];
  }

  function privacy() {
    return [h("h1", {}, "You're private by default"), h("p", { class: "lead" }, "Members see your node ID — a random key — and what you serve. Not your device, not your location, not your IP address. Want a dot on the map? That's up to you."),
      h("div", { class: "panel card" }, locationEditor()),
      h("div", { class: "hint", style: { marginTop: "10px" } }, "Change any of this later in Settings → What others see."), nav()];
  }

  function ready() {
    return [h("div", { class: "logo" }, brandMark(72, "ob2")), h("h1", {}, "You're all set."),
      h("p", { class: "lead" }, "Start a chat, add plugins from the marketplace, or connect your favourite agent to your node's local endpoint."),
      h("div", { class: "actions" }, h("button", { class: "btn glow lg", onclick: done }, "Start chatting"),
        h("button", { class: "btn lg", onclick: () => { done(); location.hash = "#/connect"; } }, icon("plug", "sm"), "Connect an agent"))];
  }

  function draw() { root.replaceChildren(h("div", { class: "ob" }, dots(), ...steps[step]())); }
  draw();
}

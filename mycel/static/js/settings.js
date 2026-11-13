import { api } from "./api.js";
import { state, on, prefs, setPref, applyTheme, refreshMesh } from "./state.js";
import { h, icon, toast, toggle, segmented, copyText, modal, nodeAvatar, short, fmt, regionName } from "./ui.js";

const SECTIONS = [["sharing", "Sharing", "cpu"], ["privacy", "Chat privacy", "lock"], ["visibility", "What others see", "eye"],
  ["membership", "Membership", "users"], ["network", "Network", "relay"], ["appearance", "Appearance", "moon"], ["about", "About", "info"]];
export const PRESETS = [[0.1, "Light", "Barely noticeable"], [0.25, "Balanced", "Recommended"], [0.5, "Generous", "Great on desktops"], [0.75, "Champion", "Dedicated machines"]];

export function render(container, section = "sharing", { go }) {
  if (section === "contribution") section = "sharing";
  if (section === "location") section = "visibility";
  const offs = [];
  const nav = h("nav", {}, SECTIONS.map(([id, label, ic]) =>
    h("button", { class: `nav-item ${id === section ? "active" : ""}`, onclick: () => go(`#/settings/${id}`) }, icon(ic), label)));
  const body = h("div");
  container.append(h("div", { class: "view" }, h("div", { class: "page", style: { maxWidth: "1000px" } },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("p", {}, "Your node, your rules."))),
    h("div", { class: "settings" }, nav, body))));

  const draw = () => body.replaceChildren(({ membership, sharing: contribution, privacy, visibility, network, appearance, about }[section] || contribution)());
  // Live-refresh only read-mostly sections, and never while the user is editing; forms keep their input.
  let lastMember = state.membership?.member;
  offs.push(on("mesh", () => {
    const memberChanged = state.membership?.member !== lastMember;
    lastMember = state.membership?.member;
    const editing = body.contains(document.activeElement) && document.activeElement !== document.body;
    if (memberChanged || (["sharing", "about"].includes(section) && !editing)) draw();
  }));
  draw();
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- membership
function membership() {
  const m = state.membership;
  if (!m) return h("div");
  const s = state.stats;
  const out = [];
  if (m.kind === "global") {
    out.push(h("div", { class: "card" },
      h("div", { style: { display: "flex", alignItems: "center", gap: "14px" } },
        h("div", { class: "picon lg", style: { background: "var(--grad)" } }, icon("globe", "lg")),
        h("div", { style: { flex: 1 } }, h("div", { style: { fontWeight: 640, fontSize: "15px" } }, "Global mesh ", meshBadge()),
          h("div", { class: "hint" }, `${m.members_online} members online · no owner, no founder — just members`)))));
    const pitch = "I'm using Mycel — a global network where people share spare GPU time so everyone gets a great AI assistant for free. Join and the mesh gets faster for all of us.";
    out.push(h("div", { class: "card", style: { background: "var(--grad-soft)" } }, h("h2", {}, "Grow the mesh"),
      h("p", {}, "Every GPU that joins adds capacity for everyone — including you. No invitation needed; they just install Mycel and tap Join."),
      s ? h("div", { class: "hint", style: { marginBottom: "12px" } }, `Right now: ${fmt(s.serving_nodes)} GPUs · ${fmt(s.sustained_tokens_per_s)} tok/s donated · ${fmt(s.regions)} regions`) : null,
      h("button", { class: "btn glow", onclick: () => copyText(pitch, "Message copied — send it to a friend") }, icon("heart", "sm"), "Copy an invite message")));
    if (m.blocked.length) out.push(h("div", { class: "card" }, h("h2", {}, "Blocked on this device"),
      h("p", {}, `${m.blocked.length} member${m.blocked.length === 1 ? "" : "s"} you blocked won't serve your chats or receive your requests.`)));
  } else if (m.kind === "private") {
    out.push(h("div", { class: "card" },
      h("div", { style: { display: "flex", alignItems: "center", gap: "14px" } }, nodeAvatar(m.mesh_id, 44),
        h("div", { style: { flex: 1 } }, h("div", { style: { fontWeight: 640, fontSize: "15px" } }, `Private mesh ${short(m.mesh_id)} `,
          m.founder ? h("span", { class: "badge grad" }, "You founded it") : h("span", { class: "badge ok" }, icon("check", "sm"), "Member")),
        h("div", { class: "hint" }, `${m.members_online} members online${m.invited_by ? ` · invited by ${short(m.invited_by)}` : ""}${m.expires ? ` · renews by ${new Date(m.expires * 1000).toLocaleDateString()}` : ""}`))),
      h("div", { style: { marginTop: "14px" } }, h("button", { class: "btn", onclick: () => switchToGlobal() }, icon("globe", "sm"), "Switch to the global mesh"))));
    if (m.can_invite) out.push(inviteCard());
  } else {
    out.push(h("div", { class: "card", style: { textAlign: "center", padding: "32px" } },
      h("div", { class: "picon lg", style: { background: "var(--grad)", margin: "0 auto 14px" } }, icon("globe", "lg")),
      h("h2", { style: { fontSize: "18px" } }, "You're not connected"),
      h("p", { style: { maxWidth: "420px", margin: "6px auto 18px" } }, "Join the global mesh to chat with models running on members' GPUs — and lend yours if you like."),
      h("button", { class: "btn glow lg", onclick: () => switchToGlobal() }, icon("globe", "sm"), "Join the global mesh")));
  }
  out.push(advanced(m));
  return h("div", { class: "grid" }, out);
}

async function switchToGlobal() {
  try { await api.post("/api/membership/join-global", {}); await refreshMesh(); toast("Connected to the global mesh"); }
  catch (e) { toast(e.message, "bad"); }
}

function advanced(m) {
  const body = h("div", { style: { marginTop: "14px" } },
    h("p", { class: "hint", style: { fontSize: "13px", marginTop: 0 } },
      "Private meshes are invitation-only networks for a team, family or community — separate from the global mesh. Your node belongs to one mesh at a time."),
    m.kind !== "private" ? [
      joinCard(m),
      h("div", { class: "card", style: { marginTop: "12px" } }, h("h2", {}, "Start a private mesh"),
        h("p", {}, "You become its founder: you invite members and can remove them. Your node leaves the global mesh while you're in it."),
        h("button", { class: "btn", onclick: () => modal({ title: "Start a private mesh?", subtitle: "Your node will leave the global mesh. You can switch back any time.",
          actions: [{ label: "Cancel", kind: "ghost" }, { label: "Create private mesh", kind: "primary", onClick: async () => {
            await api.post("/api/membership/create", {}); await refreshMesh(); toast("Your private mesh is live"); } }] }) }, icon("spark", "sm"), "Create a private mesh")),
    ] : h("div", { class: "card" }, h("h2", {}, "Leave this private mesh"), h("p", {}, "You'll disconnect from it. Rejoining needs a new invitation."),
      h("button", { class: "btn danger", onclick: () => modal({ title: "Leave the private mesh?", subtitle: "Chats stay on this device.",
        actions: [{ label: "Cancel", kind: "ghost" }, { label: "Leave", kind: "primary", onClick: async () => { await api.post("/api/membership/leave", { confirm: true }); refreshMesh(); toast("You left the mesh"); } }] }) }, "Leave mesh")));
  const det = h("details", { class: "card advanced" }, h("summary", {}, icon("key", "sm"), "Advanced · Private meshes"), body);
  if (m.kind === "private") det.open = true;
  return det;
}

export function joinCard(m, onJoined) {
  const code = h("textarea", { class: "textarea", placeholder: "Paste a membership code (mym1_…)" });
  return h("div", { class: "card" }, h("h2", {}, "Join a private mesh"),
    h("p", {}, "Send your join code to a member of that mesh, then paste the membership code they send back."),
    h("div", { class: "field" }, h("label", {}, "1 · Your join code"), h("div", { class: "code-out" }, h("div", { class: "cmd-box" }, m.join_code),
      h("button", { class: "btn sm", onclick: () => copyText(m.join_code, "Join code copied") }, icon("copy", "sm"), "Copy"))),
    h("div", { class: "field" }, h("label", {}, "2 · Membership code you received"), code),
    h("button", { class: "btn primary", onclick: async () => {
      try { await api.post("/api/membership/join", { code: code.value }); await refreshMesh(); toast("Joined the private mesh"); onJoined && onJoined(); }
      catch (e) { toast(e.message, "bad"); }
    } }, "Join private mesh"));
}

function inviteCard() {
  const code = h("textarea", { class: "textarea", placeholder: "Paste their join code (myj1_…)" });
  let canInvite = false;
  const result = h("div");
  return h("div", { class: "card" }, h("h2", {}, "Invite someone"),
    h("p", {}, "Ask them to open Mycel and copy their join code. Paste it here to create their membership."),
    h("div", { class: "field" }, h("label", {}, "Their join code"), code),
    h("div", { class: "set-row", style: { paddingTop: 0 } }, h("div", { class: "txt" }, h("b", {}, "Let them invite others"), h("span", {}, "They can grow the mesh themselves. You can still remove them later if you're the founder.")),
      toggle(false, (v) => { canInvite = v; }, "Allow inviting")),
    h("button", { class: "btn glow", onclick: async () => {
      try {
        const r = await api.post("/api/membership/invite", { join_code: code.value, can_invite: canInvite });
        result.replaceChildren(h("div", { class: "field", style: { marginTop: "16px" } }, h("label", {}, `Send this back to ${short(r.node_id)}`),
          h("div", { class: "code-out" }, h("div", { class: "cmd-box" }, r.membership_code),
            h("button", { class: "btn sm", onclick: () => copyText(r.membership_code, "Membership code copied") }, icon("copy", "sm"), "Copy")),
          h("div", { class: "hint" }, "It only works for their device — it's bound to their key, so it's safe to send over any channel.")));
      } catch (e) { toast(e.message, "bad"); }
    } }, icon("gift", "sm"), "Create membership"), result);
}

// ---------------------------------------------------------------- contribution
// Hardware problems (vs. simply "no model served yet") come from the node's eligibility check.
const hardwareOk = (st) => !st.eligibility_problems.some((p) => /GPU|memory|TFLOPS/.test(p));
export function presetPicker(current, onPick) {
  return h("div", { class: "presets" }, PRESETS.map(([v, name, desc]) => h("button", { class: `preset ${Math.abs(current - v) < 0.001 ? "on" : ""}`, onclick: () => onPick(v) },
    h("div", { class: "pct" }, `${Math.round(v * 100)}%`), h("div", { class: "nm" }, name), h("div", { class: "ds" }, desc))));
}

function contribution() {
  const st = state.status;
  if (!st) return h("div");
  const L = st.governor.limits;
  const set = async (patch, msg = "Saved") => { try { await api.post("/api/limits", patch); await refreshMesh(); toast(msg); } catch (e) { toast(e.message, "bad"); } };
  const num = (key, val, min, max) => { const i = h("input", { class: "input", type: "number", value: val, min, max, style: { width: "110px" } });
    i.addEventListener("change", () => set({ [key]: Number(i.value) })); return i; };
  const hw = st.hardware || {};
  return h("div", { class: "grid" },
    h("div", { class: "card" }, h("h2", {}, "This device"),
      h("div", { class: "kv" }, h("dt", {}, "Device"), h("dd", {}, `${hw.device_name || "–"}${hw.simulated ? " (simulated)" : ""}`),
        h("dt", {}, "GPU memory"), h("dd", {}, `${fmt(hw.accel_memory_gb, 1)} GB${hw.devices?.length > 1 ? ` across ${hw.devices.length} GPUs` : ""}`),
        h("dt", {}, "Memory bandwidth"), h("dd", {}, hw.bandwidth_gbs ? `${fmt(hw.bandwidth_gbs)} GB/s (${{ measured: "measured while serving", table: "typical for this chip", probe: "measured" }[hw.bandwidth_source] || "estimate"})` : "Measured once a model runs"),
        h("dt", {}, "Detected by"), h("dd", {}, { "llama.cpp": "llama.cpp (the engine that runs your models)", torch: "PyTorch", simulated: "simulation", none: "–" }[hw.source] || "–"),
        h("dt", {}, "Tier"), h("dd", {}, hw.tier || "–"), h("dt", {}, "Serving"), h("dd", {}, st.serving ? `${st.backend.models.join(", ")} at ${fmt(st.backend.tokens_per_s, 1)} tok/s` : st.eligibility_problems.join("; ")))),
    st.serving ? h("div", { class: "card" }, h("h2", {}, "How much of your GPU to share"),
      h("p", {}, "A hard limit on GPU time the mesh may use. Your own chats never count against it."),
      presetPicker(L.max_share, (v) => set({ max_share: v }, `Sharing ${Math.round(v * 100)}%`)),
      h("div", { class: "set-row", style: { marginTop: "8px" } }, h("div", { class: "txt" }, h("b", {}, "Share my model"), h("span", {}, "Off = private: only you use it, and the mesh can't even see its name. Swarm slices have their own switch in the cockpit.")),
        toggle(!L.paused, (v) => set({ paused: !v }, v ? "Shared with the mesh" : "Private — only for you"), "Share my model")),
      h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Simultaneous jobs"), h("span", {}, "More jobs = more throughput, more heat")), num("max_concurrent", L.max_concurrent, 0, 16)),
      h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Max reply length"), h("span", {}, "Tokens per mesh job")), num("max_tokens_per_job", L.max_tokens_per_job, 1, 8192)),
      h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Daily cap"), h("span", {}, "GPU-seconds per 24 h (0 = none)")), num("daily_seconds", L.daily_seconds, 0, 86400)),
      h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Fair use per member"), h("span", {}, "Jobs per minute any one member may start")), num("per_peer_per_min", L.per_peer_per_min, 0, 600)))
      : hardwareOk(st) ? h("div", { class: "card", style: { background: "var(--grad-soft)" } }, h("h2", {}, "Pick a model to share"),
          h("p", {}, `Your ${hw.device_name || "GPU"} qualifies to serve (${fmt(hw.accel_memory_gb, 0)} GB). Choose a model in Models — you'll chat with it locally and these limits will apply to what the mesh may use.`),
          st.model_server?.status === "error" ? h("div", { class: "warn-box" }, icon("alert"), h("div", {}, st.model_server.error)) : null,
          h("button", { class: "btn glow", onclick: () => (location.hash = "#/models") }, icon("cpu", "sm"), "Open Models"))
        : h("div", { class: "card" }, h("h2", {}, "Using without sharing"), h("p", {}, `This device can't serve models: ${st.eligibility_problems.join("; ")}. You can still use the mesh fully.`)));
}

// ---------------------------------------------------------------- privacy
export function locationEditor(onSaved) {
  const loc = state.status?.location || {};
  let share = !!loc.share, precision = loc.precision || "region";
  const lat = h("input", { class: "input", type: "number", step: "0.1", value: loc.lat ?? "", placeholder: "e.g. 6.5" });
  const lon = h("input", { class: "input", type: "number", step: "0.1", value: loc.lon ?? "", placeholder: "e.g. 3.4" });
  const preview = h("div", { class: "hint" });
  const upd = () => { preview.textContent = share ? `Peers will see: ${loc.public_cell ? regionName(loc.public_cell) + " · " : ""}a ${{ continent: "~5000 km", country: "~1250 km", region: "~156 km", metro: "~39 km" }[precision]} cell` : "Peers see nothing about your location."; };
  upd();
  return h("div", {},
    h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Show me on the network map"), h("span", {}, "Off by default. Only a coarse grid cell is ever shared.")),
      toggle(share, (v) => { share = v; upd(); }, "Share location")),
    h("div", { class: "row", style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", marginTop: "12px" } },
      h("div", { class: "field" }, h("label", {}, "Latitude"), lat), h("div", { class: "field" }, h("label", {}, "Longitude"), lon)),
    h("div", { class: "field" }, h("label", {}, "Precision"), segmented([["continent", "Continent"], ["country", "Country"], ["region", "Region"], ["metro", "Metro"]], precision, (v) => { precision = v; upd(); })),
    preview,
    h("div", { style: { marginTop: "14px" } }, h("button", { class: "btn primary", onclick: async () => {
      try { await api.post("/api/location", { share, lat: lat.value, lon: lon.value, precision }); await refreshMesh(); toast(share ? "Location shared" : "Location hidden"); onSaved && onSaved(); }
      catch (e) { toast(e.message, "bad"); }
    } }, "Save")),
    h("div", { class: "hint", style: { marginTop: "10px" } }, "Coordinates stay on this device — Mycel never looks up your location from your IP."));
}

function privacy() {
  return h("div", { class: "grid" },
    h("div", { class: "card" }, h("h2", {}, "Where your chats run"),
      h("p", {}, "This decides which GPU answers you. It has nothing to do with your location on the map."),
      h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Private first"),
        h("span", {}, "When your own GPU serves a model, it answers — your prompts never leave this device. Otherwise the mesh answers.")),
        toggle(prefs.preferLocal, (v) => { setPref("preferLocal", v); toast(v ? "Private first is on" : "Fastest node will answer"); }, "Private first"))),
    h("div", { class: "card" }, h("h2", {}, "What other members can see"),
      h("div", { class: "perm" }, icon("globe", "sm"), "One-GPU models: the node that answers a chat sees that chat's messages (including tool results you approved)."),
      h("div", { class: "perm" }, icon("layers", "sm"), "Swarm models: your device tokenizes and samples, so no node sees your words — only the activations passing through its layers."),
      h("div", { class: "perm" }, icon("lock", "sm"), "Everything between nodes is end-to-end encrypted. Relays forward ciphertext."),
      h("div", { class: "perm" }, icon("eye", "sm"), h("span", {}, "What members can learn about ", h("i", {}, "you"), " is under ", h("a", { href: "#/settings/visibility" }, "What others see"), " — private by default."))),
    h("div", { class: "card" }, h("h2", {}, "Stored only on this device"),
      h("p", {}, "Chats, notes, plugin data and your settings live in your Mycel folder and are never uploaded.")));
}

function visibility() {
  const box = h("div", { class: "grid" });
  const render = (v) => {
    const st = state.status || {};
    const relay = v.always.transport === "relay";
    const set = async (patch) => { try { render(await api.post("/api/visibility", patch)); refreshMesh(); toast("Saved"); } catch (e) { toast(e.message, "bad"); } };
    const locked = (ic, t, d) => h("div", { class: "vis-row" }, h("span", { class: "ic-tile" }, icon(ic, "sm")),
      h("div", { class: "txt" }, h("b", {}, t), h("span", {}, d)), h("span", { class: "lock" }, icon("lock", "sm"), "Always shared"));
    const opt = (ic, t, d, on, key) => h("div", { class: "vis-row" }, h("span", { class: "ic-tile" }, icon(ic, "sm")),
      h("div", { class: "txt" }, h("b", {}, t), h("span", {}, d)), toggle(on, (val) => set({ [key]: val }), t));
    box.replaceChildren(
      h("div", { class: "card" }, h("h2", {}, "Private by default"),
        h("p", {}, "Other members see only what's needed to route work to you and to verify you're a real member. Everything else is off until you turn it on."),
        locked("key", "Node ID", h("span", {}, "A random key, not your name: ", h("span", { class: "mono" }, v.always.node_id))),
        locked("shieldCheck", "Membership proof", "Shows you're a member (your proof-of-work ticket, or your invitation chain in a private mesh)."),
        locked("layers", "What you serve and how busy you are", "Models or swarm layers you host, speed, free capacity and your sharing level — so requests can find you."),
        locked(relay ? "relay" : "network", relay ? "Your relay, not your IP address" : "Your mesh address",
          relay ? "Members reach you through a relay. Your IP address never appears in what you broadcast." : "This node accepts direct connections, so its address is in its advert."),
        opt("cpu", "Device model", st.hardware?.device_name ? `Show “${st.hardware.device_name}” and exact GPU memory. Off: members see only a coarse tier (${st.hardware.tier || "–"}).` : "Show your GPU model and exact memory. Off: only a coarse tier.", v.device, "device"),
        opt("heart", "Activity", "Show roughly how many requests you've served.", v.activity, "activity"),
        h("div", { class: "vis-row" }, h("span", { class: "ic-tile" }, icon("pin", "sm")),
          h("div", { class: "txt" }, h("b", {}, "Location on the map"), h("span", {}, v.location.share ? `On: a coarse ${v.location.public_cell?.length === 1 ? "continent" : "grid"} cell (${regionName(v.location.public_cell) || "–"}). Never coordinates, never from your IP.` : "Off. Set it below if you'd like a dot on the network map.")),
          h("span", { class: "lock", style: { color: v.location.share ? "var(--good)" : "var(--text-3)" } }, icon(v.location.share ? "eye" : "eyeOff", "sm"), v.location.share ? "Shared" : "Hidden"))),
      h("div", { class: "card" }, h("h2", {}, "Your spot on the map"), h("p", {}, "Optional. Members see a coarse grid cell — never coordinates."), locationEditor(() => api.get("/api/visibility").then(render))),
      h("div", { class: "card" }, h("h2", {}, "Exactly what you broadcast"),
        h("p", {}, "Your signed advert, as every member receives it right now. Fields you've kept private simply aren't there."),
        h("pre", { class: "advert-pre" }, JSON.stringify(v.advert, null, 2))));
  };
  api.get("/api/visibility").then(render).catch((e) => box.replaceChildren(h("div", { class: "card" }, e.message)));
  return box;
}

function meshBadge() {
  const net = state.status?.network || {};
  if (net.mode === "internet" && !net.online)
    return h("a", { class: "badge", href: "#/settings/network", style: { color: "var(--warn)" } }, icon("relay", "sm"), "Finding relays");
  return h("span", { class: "badge ok" }, icon("check", "sm"), "Connected");
}

// ---------------------------------------------------------------- network (relays)
const SOURCE = { yours: ["Yours", "grad"], config: ["Start flag", ""], bundled: ["Public seed", ""], learned: ["Learned from members", ""] };

function network() {
  const box = h("div", { class: "grid" });
  const render = (v) => {
    if (v.mode !== "internet") {
      box.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Local mode"),
        h("p", {}, "This node only talks to nodes on this machine. Start Mycel without --local to reach the internet mesh through relays.")));
      return;
    }
    const member = state.status?.member;
    const set = async (path, body, ok) => { try { render(await api.post(path, body)); refreshMesh(); if (ok) toast(ok); } catch (e) { toast(e.message, "bad"); } };
    const host = (u) => { try { return new URL(u).host; } catch { return u; } };
    const dot = (r) => h("span", { class: `pulse ${r.up ? "" : r.home && member ? "warn" : "off"}`, title: r.up ? "Connected" : r.home && member ? "Connecting" : "Standby" });
    const rows = v.known.map((r) => {
      const [label, cls] = SOURCE[r.source] || [r.source, ""];
      return h("div", { class: "vis-row" }, dot(r),
        h("div", { class: "txt" }, h("b", { class: "mono" }, host(r.url)),
          h("span", {}, r.up ? `Connected${r.region ? " · " + r.region : ""}` : r.home && member ? "Connecting…" : "Standby — used if your other relays go down")),
        h("span", { class: `badge ${cls}` }, label),
        r.source === "yours" ? h("button", { class: "btn sm ghost", title: "Remove", onclick: () => set("/api/network/relays/remove", { url: r.url }, "Relay removed") }, icon("trash", "sm")) : null);
    });
    const input = h("input", { class: "input", placeholder: "wss://relay.example.org/ws", spellcheck: "false" });
    const add = () => input.value.trim() && set("/api/network/relays", { url: input.value.trim() }, "Relay added");
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    const headline = !member ? ["Not connected", "Join a mesh and Mycel connects to these relays. Until then it contacts none of them."]
      : v.online ? ["Connected to the internet mesh", `Through ${v.relays.filter((r) => r.up).length} relay${v.relays.filter((r) => r.up).length === 1 ? "" : "s"}. Your device opens no ports.`]
      : v.known.length ? ["Finding the mesh", "Trying your relays. Mycel keeps retrying and switches to another relay automatically."]
      : ["No relays yet", "This build ships without public relays. Add one a friend or your community runs, below."];
    box.replaceChildren(
      h("div", { class: "card" },
        h("div", { style: { display: "flex", alignItems: "center", gap: "14px" } },
          h("div", { class: "picon lg", style: { background: v.online ? "var(--grad)" : "var(--line-2)" } }, icon("relay", "lg")),
          h("div", { style: { flex: 1 } }, h("div", { style: { fontWeight: 640, fontSize: "15px" } }, headline[0]), h("div", { class: "hint" }, headline[1])))),
      h("div", { class: "card" }, h("h2", {}, "Relays"),
        h("p", {}, "Relays are small forwarders that connect members behind home routers. They only ever see encrypted frames. They can see your IP address, never your chats."),
        rows.length ? rows : h("p", { class: "hint" }, "None known yet."),
        h("div", { class: "field", style: { marginTop: "14px" } }, h("label", {}, "Add a relay"),
          h("div", { style: { display: "flex", gap: "8px" } }, input, h("button", { class: "btn primary", onclick: add }, icon("plus", "sm"), "Add")))),
      h("div", { class: "card" }, h("h2", {}, "Public seed relays"),
        h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Use the relays that ship with Mycel"),
          h("span", {}, v.has_seeds || !v.use_bundled ? "Needed only for the first connection; after that Mycel also uses relays it learns from members." : "This build doesn't include any yet.")),
          toggle(v.use_bundled, (on) => set("/api/network/bundled", { on }, on ? "Seed relays on" : "Seed relays off"), "Use seed relays"))));
  };
  api.get("/api/network").then(render).catch((e) => box.replaceChildren(h("div", { class: "card" }, e.message)));
  return box;
}

function appearance() {
  return h("div", { class: "card" }, h("h2", {}, "Theme"),
    h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Appearance"), h("span", {}, "Match your system or pick one")),
      segmented([["system", "System"], ["light", "Light"], ["dark", "Dark"]], prefs.theme, (v) => { setPref("theme", v); applyTheme(); })));
}

function about() {
  const st = state.status || {};
  return h("div", { class: "card" }, h("h2", {}, "About Mycel"),
    h("div", { class: "kv", style: { marginTop: "12px" } }, h("dt", {}, "Version"), h("dd", {}, st.version || "–"), h("dt", {}, "Node id"), h("dd", { class: "mono" }, st.node_id || "–"),
      h("dt", {}, "Local endpoint"), h("dd", { class: "mono" }, `${location.origin}/v1`), h("dt", {}, "Uptime"), h("dd", {}, `${fmt((st.uptime_s || 0) / 60)} min`)),
    h("div", { style: { marginTop: "16px" } }, h("button", { class: "btn", onclick: () => { setPref("onboarded", false); location.reload(); } }, "Replay the welcome tour")));
}

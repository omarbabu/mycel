// "Your place in the mesh": one source of truth for what your node is doing, the live orb,
// and the header/sidebar controls. Every indicator takes you to the control that changes it:
// the cockpit at the top of Network.
import { state, on, prefs, setPref, modelName } from "./state.js";
import { h, icon, popover, closePopover, toast } from "./ui.js";

// Three ways to take part (using the mesh is always on):
//   private — a model runs on your GPU for you only        sharing/helping — the mesh may use it too
//   swarm   — you host a slice of a giant model             idle — nothing on your GPU; you use the mesh
export function nodeState() {
  const st = state.status;
  if (!state.online || !st) return { key: "offline", title: "Node offline", sub: "Mycel isn't running on this device" };
  if (!st.member) return { key: "off", title: "Not connected", sub: "Join the global mesh to start" };
  const srv = st.model_server || {};
  const sw = st.swarm || {};
  const slice = sw.model && sw.span ? `${modelName(sw.model)} · layers ${sw.span[0] + 1}–${sw.span[1]}` : null;
  if (["downloading", "loading", "ready"].includes(srv.status) && !st.serving) {
    const pct = srv.progress != null ? ` · ${Math.round(srv.progress * 100)}%` : "";
    return { key: "busy", title: srv.status === "downloading" ? "Downloading your model" : "Loading your model", sub: `${modelName(srv.model_id)}${pct}` };
  }
  const net = st.network || {};
  if (net.mode === "internet" && !net.online) {
    const known = (net.known || []).length;
    return known ? { key: "searching", title: "Finding the mesh", sub: `Connecting to ${known === 1 ? "a relay" : known + " relays"}…` }
                 : { key: "searching", title: "No relays yet", sub: "Add a relay in Settings → Network" };
  }
  const g = st.governor;
  const model = st.serving ? modelName(st.backend.models[0]) : null;
  if (st.serving && st.sharing && g.active_jobs > 0)
    return { key: "helping", title: `Helping ${g.active_jobs === 1 ? "someone" : g.active_jobs + " people"} right now`, sub: `${model}${slice ? " + a swarm slice" : ""}` };
  if (st.serving && st.sharing) return { key: "sharing", title: "Sharing with the mesh", sub: `${model} · up to ${Math.round(g.limits.max_share * 100)}%${slice ? " + slice" : ""}` };
  if (slice) return { key: "swarm", title: "Hosting a swarm slice", sub: slice };
  if (sw.model) return { key: "busy", title: "Joining a swarm", sub: `${modelName(sw.model)} · finding your layers` };
  if (st.serving) return { key: "private", title: "Private model", sub: `${model} · only for you` };
  return { key: "idle", title: "Using the mesh", sub: "Nothing running on your GPU" };
}

// Subscribe an element to mesh updates for as long as it's on screen.
export function live(el, paint) {
  paint();
  let seen = false;
  const off = on("mesh", () => { if (el.isConnected) { seen = true; paint(); } else if (seen) off(); });
}

export function orb(size = 30) {
  const el = h("span", { class: "orb", style: { width: size + "px", height: size + "px" } }, h("i"));
  live(el, () => { el.dataset.state = nodeState().key; });
  return el;
}

export function goCockpit() {
  if (location.hash.startsWith("#/network")) document.querySelector(".cockpit")?.scrollIntoView({ behavior: "smooth", block: "start" });
  else location.hash = "#/network";
}

const SHORT = { offline: "Offline", off: "Join mesh", busy: "Setting up", searching: "Connecting", idle: "Using mesh", private: "Private", swarm: "Swarm host",
  helping: "Helping now", sharing: "Sharing" };

// Header control: orb + state. Opens your node's cockpit.
export function nodeButton({ label = true } = {}) {
  const text = h("span", { class: "nb-text" });
  const btn = h("button", { class: "node-btn", title: "Your node" }, orb(26), label ? text : null);
  live(btn, () => { const s = nodeState(); btn.dataset.state = s.key; text.textContent = SHORT[s.key]; btn.title = `${s.title} — ${s.sub}. Open your node.`; });
  btn.addEventListener("click", goCockpit);
  return btn;
}

// The composer's "where will this run" chip: choose private-first or fastest, right there.
export function routePopover(anchor) {
  const st = state.status || {};
  const choice = (v, ic, t, d) => h("button", { class: `opt ${prefs.preferLocal === v ? "kb" : ""}`, role: "menuitemradio", "aria-checked": String(prefs.preferLocal === v),
    onclick: () => { closePopover(); setPref("preferLocal", v); toast(v ? "Private first: your GPU answers when it can" : "Fastest: the best node answers"); } },
  icon(ic, "sm"), h("div", {}, h("div", { class: "main-t" }, t), h("div", { class: "sub-t" }, d)), prefs.preferLocal === v ? h("span", { class: "right" }, icon("check", "sm")) : null);
  popover(anchor, [h("div", { class: "label" }, "Where your chats run"),
    choice(true, "lock", "Private first", st.serving ? `Your GPU's ${modelName(st.backend.models[0])} answers — nothing leaves this device` : "Your GPU, once a model is active on it"),
    choice(false, "globe", "Fastest", "The best model and node on the mesh"),
    h("hr"), h("button", { class: "opt", onclick: () => { closePopover(); goCockpit(); } }, icon("cpu", "sm"), h("span", { class: "main-t" }, "Your node & sharing →"))],
  { above: true });
}

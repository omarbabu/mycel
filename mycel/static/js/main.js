import { api } from "./api.js";
import { state, on, prefs, applyTheme, refreshMesh, refreshImpact, refreshConversations, refreshPlugins, refreshLibrary } from "./state.js";
import { h, icon, brandMark, nodeAvatar, compact, closePopover, popover, modal, toast } from "./ui.js";
import * as chat from "./chat.js";
import * as network from "./network.js";
import * as models from "./models.js";
import * as plugins from "./plugins.js";
import * as connect from "./connect.js";
import * as settings from "./settings.js";
import { showOnboarding } from "./onboarding.js";
import { orb, nodeState, goCockpit } from "./nodepanel.js";
import { aurora } from "./gl.js";

export const APP_VERSION = "0.5.0";
document.body.prepend(aurora(() => nodeState().key));

applyTheme();
matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyTheme);

const VIEWS = { chat, models, network, plugins, connect, settings };
const root = document.getElementById("app");
let cleanup = null;

// ---------------------------------------------------------------- sidebar
const convList = h("div", { class: "conv-list", role: "list" });
const search = { value: "" };  // chat search lives in the ⌘K palette
const navBtns = {};
function navItem(route, label, ic, extra) {
  const b = h("button", { class: "nav-item", onclick: () => go("#/" + route) }, icon(ic), label, extra || null);
  navBtns[route] = b;
  return b;
}
const pluginCount = h("span", { class: "count" });
const nodeCount = h("span", { class: "count" });
const nodeCard = h("button", { class: "node-card", "aria-label": "Your node — open the cockpit", onclick: () => goCockpit() });

const sidebar = h("aside", { class: "sidebar" },
  h("div", { class: "brand" }, brandMark(26, "sb"), h("span", { class: "brand-name" }, "Mycel"),
    h("button", { class: "icon-btn sm brand-collapse", "aria-label": "Collapse sidebar", title: "Collapse sidebar (⌘\\)", onclick: () => toggleSidebar() }, icon("sidebar", "sm"))),
  h("div", { class: "side-actions" },
    h("button", { class: "btn-new", onclick: () => newChat() }, icon("edit"), "New chat"),
    h("button", { class: "side-search-btn", onclick: () => commandPalette(), "aria-label": "Search" }, icon("search", "sm"), "Search", h("kbd", {}, "⌘K")),
  ),
  h("div", { class: "side-section" }, "Recent"),
  convList,
  h("div", { class: "side-nav" },
    navItem("models", "Models", "cpu"),
    navItem("network", "Network", "network", nodeCount),
    navItem("plugins", "Plugins", "puzzle", pluginCount),
    navItem("connect", "Connect an agent", "plug"),
    navItem("settings", "Settings", "sliders"),
  ),
  h("div", { class: "side-foot" }, nodeCard),
);
const main = h("main", { class: "main" });
const shell = h("div", { class: "app" }, sidebar, main);
root.replaceChildren(shell);
const SB = "mycel.sidebar";
shell.classList.toggle("sb-collapsed", localStorage.getItem(SB) === "collapsed");
function toggleSidebar() {
  const c = shell.classList.toggle("sb-collapsed");
  localStorage.setItem(SB, c ? "collapsed" : "open");
}
document.addEventListener("mycel:toggle-sidebar", toggleSidebar);

function groupLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Previous 7 days" : days < 30 ? "Previous 30 days" : "Older";
}
function renderConvs() {
  const q = search.value.trim().toLowerCase();
  const active = location.hash.split("/")[2];
  const items = state.conversations.filter((c) => !q || c.title.toLowerCase().includes(q) || c.preview.toLowerCase().includes(q));
  const out = [];
  let group = null;
  for (const c of items) {
    const g = c.pinned ? "Pinned" : groupLabel(c.updated);
    if (g !== group) { out.push(h("div", { class: "conv-group" }, g)); group = g; }
    out.push(h("div", { class: `conv ${c.id === active ? "active" : ""}`, role: "listitem", tabindex: 0,
      onclick: () => go(`#/chat/${c.id}`), onkeydown: (e) => { if (e.key === "Enter") go(`#/chat/${c.id}`); } },
    h("span", { class: "t" }, c.title),
    h("span", { class: "acts" },
      h("button", { class: "icon-btn sm", title: "More", "aria-label": "Chat options", onclick: (e) => { e.stopPropagation(); convMenu(e.currentTarget, c); } }, icon("down", "sm")))));
  }
  if (!items.length) out.push(h("div", { class: "conv-group" }, q ? "No matches" : "Your chats will appear here"));
  convList.replaceChildren(...out);
}
function convMenu(anchor, c) {
  popover(anchor, [
    h("button", { class: "opt", onclick: async () => { closePopover(); await api.put(`/api/conversations/${c.id}`, { pinned: !c.pinned }); refreshConversations(); } },
      icon("star", "sm"), h("span", { class: "main-t" }, c.pinned ? "Unpin" : "Pin")),
    h("button", { class: "opt", onclick: () => { closePopover(); renameChat(c); } }, icon("edit", "sm"), h("span", { class: "main-t" }, "Rename")),
    h("hr"),
    h("button", { class: "opt", style: { color: "var(--bad)" }, onclick: async () => {
      closePopover(); await api.del(`/api/conversations/${c.id}`); await refreshConversations();
      if (location.hash.includes(c.id)) go("#/chat"); toast("Chat deleted"); } }, icon("trash", "sm"), h("span", { class: "main-t" }, "Delete")),
  ], { align: "right" });
}
function renameChat(c) {
  const input = h("input", { class: "input", value: c.title });
  modal({ title: "Rename chat", body: input, actions: [
    { label: "Cancel", kind: "ghost" },
    { label: "Save", kind: "primary", onClick: async () => { await api.put(`/api/conversations/${c.id}`, { title: input.value.trim() || c.title }); refreshConversations(); } },
  ] });
}

let banner = null;
function versionBanner() {
  const v = state.status?.version;
  if (!v || v === APP_VERSION) { banner?.remove(); banner = null; return; }
  if (banner) return;
  banner = h("div", { class: "update-banner", role: "status" }, icon("refresh", "sm"),
    h("span", {}, h("b", {}, "Finish updating Mycel. "), `This app is ${APP_VERSION}, but your node is still running ${v} — some controls won't respond until you restart it (Ctrl-C, then run `, h("span", { class: "mono" }, "mycel"), ")."));
  document.body.appendChild(banner);
}
function renderSideStatus() {
  versionBanner();
  const s = state.stats, st = state.status;
  nodeCount.textContent = s && s.active_nodes ? `${s.active_nodes}` : "";
  pluginCount.textContent = state.plugins.length ? `${state.plugins.filter((p) => p.enabled).length}` : "";
  if (!st && state.online) return;
  const ns = nodeState();
  nodeCard.dataset.state = ns.key;
  nodeCard.replaceChildren(orb(30), h("div", { class: "meta" }, h("div", { class: "l1" }, ns.title), h("div", { class: "l2" }, ns.sub)));
}

// ---------------------------------------------------------------- routing
export function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
async function newChat() { go("#/chat"); }
function route() {
  closePopover();
  const [, name = "chat", param] = (location.hash || "#/chat").split("/");
  const view = VIEWS[name] || chat;
  for (const [k, b] of Object.entries(navBtns)) b.classList.toggle("active", k === name);
  if (cleanup) cleanup();
  main.replaceChildren();
  cleanup = view.render(main, param, { go }) || null;
  renderConvs();
}
addEventListener("hashchange", route);

// ---------------------------------------------------------------- command palette
function commandPalette() {
  const input = h("input", { placeholder: "Search chats and actions…", "aria-label": "Command" });
  const list = h("div", { class: "list" });
  const actions = [
    { t: "New chat", ic: "edit", run: () => go("#/chat") },
    { t: "Models — pick what to serve", ic: "cpu", run: () => go("#/models") },
    { t: "Network", ic: "network", run: () => go("#/network") },
    { t: "Plugins & marketplace", ic: "puzzle", run: () => go("#/plugins") },
    { t: "Connect an agent", ic: "plug", run: () => go("#/connect") },
    { t: "Grow the mesh", ic: "heart", run: () => go("#/settings/membership") },
    { t: "Sharing settings", ic: "cpu", run: () => go("#/settings/sharing") },
    { t: "Chat privacy", ic: "lock", run: () => go("#/settings/privacy") },
    { t: "What others see about me", ic: "eye", run: () => go("#/settings/visibility") },
    { t: "Host a slice of a big model (swarm)", ic: "layers", run: () => go("#/models") },
    { t: "Toggle sidebar", ic: "sidebar", run: () => toggleSidebar() },
    { t: "Your node — cockpit", ic: "cpu", run: () => go("#/network") },
    { t: state.status?.governor.limits.paused ? "Share my model with the mesh" : "Make my model private", ic: state.status?.governor.limits.paused ? "globe" : "lock",
      run: async () => { await api.post("/api/limits", { paused: !state.status.governor.limits.paused }); refreshMesh(); } },
  ];
  let sel = 0, items = [];
  const m = modal({ title: "", body: h("div", {}, input, list) });
  m.el.classList.add("cmdk");
  m.el.querySelector(".modal-head").remove();
  m.el.querySelector(".modal-body").style.padding = "0";
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    items = [
      ...actions.filter((a) => !q || a.t.toLowerCase().includes(q)),
      ...state.conversations.filter((c) => q && c.title.toLowerCase().includes(q)).slice(0, 8)
        .map((c) => ({ t: c.title, ic: "chat", sub: "Chat", run: () => go(`#/chat/${c.id}`) })),
    ];
    sel = Math.min(sel, Math.max(0, items.length - 1));
    list.replaceChildren(...items.map((it, i) => h("button", { class: `opt ${i === sel ? "kb" : ""}`, onclick: () => { m.close(); it.run(); } },
      icon(it.ic, "sm"), h("span", { class: "main-t" }, it.t), it.sub ? h("span", { class: "right sub-t" }, it.sub) : null)));
    list.prepend(h("div", { class: "label" }, q ? "Results" : "Quick actions"));
  };
  input.addEventListener("input", () => { sel = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); draw(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); e.preventDefault(); }
    if (e.key === "Enter" && items[sel]) { m.close(); items[sel].run(); }
  });
  draw();
  setTimeout(() => input.focus(), 20);
}
addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); commandPalette(); }
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); go("#/chat"); }
  if ((e.metaKey || e.ctrlKey) && e.key === "\\") { e.preventDefault(); toggleSidebar(); }
});

// ---------------------------------------------------------------- boot
on("mesh", renderSideStatus);
on("impact", renderSideStatus);
on("plugins", renderSideStatus);
on("convs", renderConvs);
await Promise.all([refreshMesh(), refreshConversations(), refreshPlugins(), refreshImpact(), refreshLibrary()]);
on("library", () => { renderSideStatus(); });
route();
setInterval(refreshMesh, 2500);
setInterval(refreshImpact, 10000);
if (!prefs.onboarded) showOnboarding();

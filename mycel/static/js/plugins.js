import { api } from "./api.js";
import { state, on, refreshPlugins } from "./state.js";
import { h, icon, modal, toast, toggle, segmented, pluginIcon } from "./ui.js";

function shellSplit(s) {
  const out = [];
  let cur = "", q = null, any = false;
  for (const c of s.trim()) {
    if (q) { if (c === q) q = null; else cur += c; continue; }
    if (c === '"' || c === "'") { q = c; any = true; continue; }
    if (/\s/.test(c)) { if (cur || any) out.push(cur); cur = ""; any = false; continue; }
    cur += c;
  }
  if (cur || any) out.push(cur);
  return out;
}

const verifiedBadge = (p) => p.verified
  ? h("span", { class: "pub" }, icon("shieldCheck", "sm"), p.publisher)
  : h("span", { class: "pub", style: { color: "var(--warn)" } }, icon("alert", "sm"), p.publisher);

export function render(container) {
  let query = "", cat = "All";
  const offs = [];
  const featured = h("div", { class: "featured" });
  const grid = h("div", { class: "market" });
  const chips = h("div", { class: "chips" });
  const searchInput = h("input", { class: "input", placeholder: "Search plugins", style: { maxWidth: "280px" },
    oninput: (e) => { query = e.target.value.toLowerCase(); draw(); } });

  container.append(h("div", { class: "view" }, h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Plugins"),
      h("p", {}, "Give the assistant new abilities. Plugins run on your device, and you approve anything sensitive.")),
    h("div", { class: "spacer" }), searchInput),
    h("div", { class: "side-section", style: { padding: "0 0 10px" } }, "Featured"),
    featured,
    h("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "14px" } }, chips),
    grid)));

  function actionButton(p) {
    if (!p.installed) return h("button", { class: "btn sm primary", onclick: (e) => { e.stopPropagation(); install(p); } }, "Get");
    return h("span", { class: "badge ok" }, icon("check", "sm"), p.enabled ? "Installed" : "Disabled");
  }

  function draw() {
    const cats = ["All", "Installed", ...new Set(state.plugins.map((p) => p.category))];
    chips.replaceChildren(...cats.map((c) => h("button", { class: c === cat ? "on" : "", onclick: () => { cat = c; draw(); } }, c)));
    featured.replaceChildren(...state.plugins.filter((p) => p.featured).slice(0, 3).map((p) =>
      h("button", { class: "feat", onclick: () => details(p) }, pluginIcon(p, "lg"),
        h("div", { class: "n" }, p.name), h("div", { class: "tag" }, p.tagline),
        h("div", { style: { marginTop: "14px", display: "flex", gap: "8px", alignItems: "center" } }, actionButton(p), verifiedBadge(p)))));
    const list = state.plugins.filter((p) => (cat === "All" || (cat === "Installed" ? p.installed : p.category === cat))
      && (!query || `${p.name} ${p.tagline} ${p.category}`.toLowerCase().includes(query)));
    grid.replaceChildren(...list.map((p) => h("button", { class: "pcard", onclick: () => details(p) },
      h("div", { class: "top" }, pluginIcon(p), h("div", {}, h("div", { class: "n" }, p.name), verifiedBadge(p))),
      h("div", { class: "tag" }, p.tagline),
      h("div", { class: "bot" }, h("span", { class: "badge" }, p.category), p.kind === "mcp" ? h("span", { class: "badge" }, "MCP") : null,
        h("span", { style: { marginLeft: "auto" } }, actionButton(p))))),
    h("button", { class: "pcard", onclick: addCustom, style: { borderStyle: "dashed", alignItems: "center", justifyContent: "center", minHeight: "150px" } },
      h("div", { class: "picon", style: { background: "var(--panel-3)", color: "var(--text-2)" } }, icon("plus")),
      h("div", { class: "n" }, "Add an MCP server"), h("div", { class: "tag", style: { textAlign: "center", flex: 0 } }, "Connect any Model Context Protocol server you trust.")));
  }

  async function install(p) {
    const config = {};
    const doInstall = async () => {
      let command = null;
      try { command = (await api.post("/api/plugins/preview", { id: p.id, config })).command; }
      catch (e) { toast(e.message, "bad"); return false; }
      if (!command) { await api.post("/api/plugins/install", { id: p.id }); toast(`${p.name} installed`); await refreshPlugins(); return true; }
      return new Promise((resolve) => {
        modal({ title: `Run ${p.name} on this device?`, iconEl: pluginIcon(p),
          subtitle: "This plugin is a program that runs with your user account's permissions.",
          body: h("div", {}, h("div", { class: "hint", style: { marginBottom: "6px" } }, "Exact command"), h("div", { class: "cmd-box" }, command.join(" ")),
            h("div", { class: "warn-box" }, icon("alert"), h("div", {}, "Only install plugins from publishers you trust. Mycel starts it with a clean environment (no API keys or cloud credentials) and asks before each sensitive action, but the program itself is not sandboxed.")),
            h("div", { class: "hint" }, "Requires ", h("b", {}, command[0]), " to be installed on this machine.")),
          actions: [{ label: "Cancel", kind: "ghost", onClick: () => resolve(false) },
            { label: "Install", kind: "primary", onClick: async () => {
              try { await api.post("/api/plugins/install", { id: p.id, config, confirm_command: command }); toast(`${p.name} installed`); await refreshPlugins(); resolve(true); }
              catch (e) { toast(e.message, "bad"); resolve(false); }
            } }],
          onClose: () => resolve(false) });
      });
    };
    if (!p.config.length) return doInstall();
    const inputs = p.config.map((f) => { const i = h("input", { class: "input", placeholder: f.type === "directory" ? "/Users/you/Projects/notes" : "" }); return [f, i]; });
    modal({ title: `Set up ${p.name}`, iconEl: pluginIcon(p), subtitle: p.tagline,
      body: h("div", {}, inputs.map(([f, i]) => h("div", { class: "field" }, h("label", {}, f.label), i,
        f.type === "directory" ? h("div", { class: "hint" }, "Paste the full path of a folder. The plugin can't see anything outside it.") : null))),
      actions: [{ label: "Cancel", kind: "ghost" }, { label: "Continue", kind: "primary", onClick: async () => {
        for (const [f, i] of inputs) config[f.key] = i.value.trim();
        return (await doInstall()) ? undefined : false;
      } }] });
  }

  function addCustom() {
    const name = h("input", { class: "input", placeholder: "My MCP server" });
    const cmd = h("input", { class: "input mono", placeholder: "npx -y @scope/my-mcp-server --flag value" });
    const ack = h("input", { type: "checkbox" });
    modal({ title: "Add an MCP server", subtitle: "Any program that speaks the Model Context Protocol over stdio.",
      body: h("div", {}, h("div", { class: "field" }, h("label", {}, "Name"), name),
        h("div", { class: "field" }, h("label", {}, "Command"), cmd, h("div", { class: "hint" }, "Runs directly — no shell, so pipes and variables don't apply.")),
        h("div", { class: "warn-box" }, icon("alert"), h("div", {}, "Custom servers are unverified and run with your account's permissions.")),
        h("label", { style: { display: "flex", gap: "8px", alignItems: "center", fontSize: "13.5px" } }, ack, "I trust this program")),
      actions: [{ label: "Cancel", kind: "ghost" }, { label: "Add server", kind: "primary", onClick: async () => {
        if (!ack.checked) { toast("Confirm you trust this program first", "bad"); return false; }
        try { await api.post("/api/plugins/custom", { name: name.value, command: shellSplit(cmd.value), confirm: true }); toast("Server added"); await refreshPlugins(); }
        catch (e) { toast(e.message, "bad"); return false; }
      } }] });
  }

  function details(p) {
    const tools = state.tools.filter((t) => t.plugin === p.id && t.spec);
    const err = state.tools.find((t) => t.plugin === p.id && t.error);
    const body = h("div", {},
      h("div", { class: "dim", style: { marginBottom: "16px" } }, p.description),
      h("div", { class: "side-section", style: { padding: "0 0 4px" } }, "Permissions"),
      p.permissions.map((x) => h("div", { class: "perm" }, icon(x.startsWith("Runs a program") ? "terminal" : x.includes("sent") ? "globe" : "lock", "sm"), x)),
      p.installed ? [
        h("div", { class: "side-section", style: { padding: "16px 0 4px" } }, "Settings"),
        h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "Enabled"), h("span", {}, "Make this plugin available in chats")),
          toggle(p.enabled, async (v) => { await api.post("/api/plugins/update", { id: p.id, enabled: v }); refreshPlugins(); }, "Enabled")),
        h("div", { class: "set-row" }, h("div", { class: "txt" }, h("b", {}, "When the assistant wants to use it"), h("span", {}, "You can change this any time")),
          segmented([["ask", "Ask first"], ["auto", "Run automatically"]], p.permission, async (v) => {
            await api.post("/api/plugins/update", { id: p.id, permission: v }); refreshPlugins(); toast("Saved"); })),
        tools.length ? [h("div", { class: "side-section", style: { padding: "16px 0 4px" } }, `Tools (${tools.length})`),
          tools.map((t) => h("div", { class: "perm" }, icon("code", "sm"), h("div", {}, h("b", { class: "mono" }, t.tool), h("div", { class: "hint" }, t.spec.function.description))))] : null,
        err ? h("div", { class: "warn-box" }, icon("alert"), h("div", {}, `Couldn't start: ${err.error}`)) : null,
      ] : null);
    const actions = p.installed
      ? [{ label: "Uninstall", kind: "ghost danger", onClick: async () => { await api.post("/api/plugins/uninstall", { id: p.id }); toast(`${p.name} removed`); refreshPlugins(); } },
         { label: "Done", kind: "primary" }]
      : [{ label: "Close", kind: "ghost" }, { label: "Get", kind: "primary", onClick: () => { install(p); } }];
    modal({ title: p.name, subtitle: p.tagline, iconEl: pluginIcon(p, "lg"), body, actions, wide: false });
  }

  offs.push(on("plugins", draw));
  refreshPlugins().then(draw);
  draw();
  return () => offs.forEach((f) => f());
}

// Shared app state, preferences and a tiny event bus.
import { api } from "./api.js";

export const state = {
  status: null, stats: null, nodes: [], models: [], auto: null, membership: null,
  impact: null, conversations: [], tools: [], plugins: [], online: true, library: null,
};
const bus = new EventTarget();
export const on = (name, fn) => { const h = (e) => fn(e.detail); bus.addEventListener(name, h); return () => bus.removeEventListener(name, h); };
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

const PREFS = "mycel.prefs";
const defaults = { theme: "system", preferLocal: true, model: "auto", onboarded: false, maxTokens: 2048 };
export const prefs = { ...defaults, ...JSON.parse(localStorage.getItem(PREFS) || "{}") };
export function setPref(k, v) { prefs[k] = v; localStorage.setItem(PREFS, JSON.stringify(prefs)); emit("prefs", k); }

export function applyTheme() {
  const t = prefs.theme;
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}

export async function refreshMesh() {
  try {
    const [status, stats, nodes, models, membership] = await Promise.all([
      api.get("/api/status"), api.get("/api/stats"), api.get("/api/nodes"), api.get("/api/models"), api.get("/api/membership")]);
    Object.assign(state, { status, stats, nodes: nodes.nodes, models: models.models, auto: models.auto, membership, online: true });
  } catch {
    state.online = false;
  }
  emit("mesh");
}
export async function refreshImpact() {
  try { state.impact = await api.get("/api/impact"); emit("impact"); } catch { /* offline */ }
}
export async function refreshConversations() {
  try { state.conversations = (await api.get("/api/conversations")).conversations; emit("convs"); } catch { /* offline */ }
}
export async function refreshPlugins() {
  try {
    const [cat, tools] = await Promise.all([api.get("/api/plugins"), api.get("/api/plugins/tools")]);
    state.plugins = cat.plugins; state.tools = tools.tools; emit("plugins");
  } catch { /* offline */ }
}
export async function refreshLibrary() {
  try { state.library = await api.get("/api/library"); emit("library"); emit("mesh"); } catch { /* offline */ }
}
export const SWARM = "@swarm";
export const isSwarm = (id) => typeof id === "string" && id.endsWith(SWARM);
export const baseId = (id) => (isSwarm(id) ? id.slice(0, -SWARM.length) : id);
export const modelName = (id) => {
  const b = baseId(id);
  const name = state.library?.models.find((m) => m.id === b)?.name || state.models.find((m) => m.id === b)?.name || b;
  return isSwarm(id) ? `${name} · Swarm` : name;
};
// Swarm overview for a model (coverage, hosts, speed) or null.
export const swarmOf = (id) => state.models.find((m) => m.id === baseId(id))?.swarm || null;

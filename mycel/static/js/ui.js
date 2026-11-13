// UI primitives. Every piece of peer- or model-supplied text goes through textContent.
const SVGNS = "http://www.w3.org/2000/svg";

export function h(tag, attrs = {}, ...kids) {
  const svg = tag.startsWith("svg:");
  const e = svg ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.setAttribute("class", v);
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
  }
  return e;
}

// ---------------------------------------------------------------- icons (24px stroke set)
const P = {
  plus: "M12 5v14M5 12h14",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-3.5-3.5",
  chat: "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z",
  network: "M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9zM12 3v18M4.2 7.5L19.8 16.5M19.8 7.5L4.2 16.5",
  puzzle: "M9 3h4v2.5a1.5 1.5 0 1 0 3 0V3h3a2 2 0 0 1 2 2v3h-2.5a1.5 1.5 0 1 0 0 3H21v3a2 2 0 0 1-2 2h-3v-2.5a1.5 1.5 0 1 0-3 0V16H9a2 2 0 0 1-2-2v-3H4.5a1.5 1.5 0 1 1 0-3H7V5a2 2 0 0 1 2-2z",
  plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  sidebar: "M4 5h16v14H4zM9.5 5v14",
  send: "M12 19V5M5 12l7-7 7 7",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  stop: "M7 7h10v10H7z",
  copy: "M9 9h10v10H9zM5 15V5h10",
  check: "M5 12.5l4.5 4.5L19 7",
  refresh: "M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4",
  down: "M6 9l6 6 6-6",
  x: "M6 6l12 12M18 6L6 18",
  spark: "M12 3l1.8 5.4L19 10l-5.2 1.6L12 17l-1.8-5.4L5 10l5.2-1.6zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  shield: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z",
  shieldCheck: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6zM8.5 12l2.5 2.5 4.5-5",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9S14.5 18.3 12 21c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z",
  cpu: "M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3",
  pin: "M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11zM12 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  star: "M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z",
  edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  calculator: "M6 3h12v18H6zM9 7h6M9 12h.01M12 12h.01M15 12h.01M9 16h.01M12 16h.01M15 16h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  ruler: "M3 17L17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2",
  note: "M5 3h10l4 4v14H5zM15 3v4h4M8 12h8M8 16h5",
  folder: "M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z",
  branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9c0 6-12 3-12 9",
  graph: "M5 6a2 2 0 1 0 0-.01M19 6a2 2 0 1 0 0-.01M12 19a2 2 0 1 0 0-.01M6.7 7.2l4.2 9.8M17.3 7.2l-4.2 9.8M7 6h10",
  mesh: "M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9zM12 8.5l3 1.75v3.5L12 15.5l-3-1.75v-3.5z",
  "globe-clock": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M16 3.5a4 4 0 0 1 0 7.5M18 14a6 6 0 0 1 4 6",
  key: "M15 9a4 4 0 1 1-8 0 4 4 0 0 1 8 0zM13.5 12l7.5 7.5M18 16.5l2-2",
  heart: "M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.8C19.5 15.4 12 20 12 20z",
  terminal: "M4 5h16v14H4zM8 10l3 2-3 2M13 15h4",
  code: "M9 8l-4 4 4 4M15 8l4 4-4 4",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  alert: "M12 4l9 16H3zM12 10v4M12 17h.01",
  moon: "M20 14.5A8 8 0 1 1 9.5 4 6.5 6.5 0 0 0 20 14.5z",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  pause: "M8 5h3v14H8zM13 5h3v14h-3z",
  play: "M7 4l13 8-13 8z",
  gift: "M4 10h16v10H4zM3 7h18v3H3zM12 7v13M12 7c-1.5-3-5-3.5-5-1s3.5 1 5 1zM12 7c1.5-3 5-3.5 5-1s-3.5 1-5 1z",
  layers: "M12 3l9 5-9 5-9-5zM3 12.5l9 5 9-5M3 16.5l9 5 9-5",
  eye: "M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7S2.5 12 2.5 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  eyeOff: "M3 3l18 18M10.6 5.1A9.9 9.9 0 0 1 12 5c6 0 9.5 7 9.5 7a17 17 0 0 1-3 3.9M6.6 6.6A16.6 16.6 0 0 0 2.5 12S6 19 12 19a9.3 9.3 0 0 0 4.4-1.1M9.9 9.9a3 3 0 0 0 4.2 4.2",
  route: "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 15V9a3 3 0 0 1 3-3h6M18 9v6a3 3 0 0 1-3 3H9",
  relay: "M4 12h4M16 12h4M12 4v4M12 16v4M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
};
export function icon(name, cls = "") {
  const s = h("svg:svg", { class: `i ${cls}`, viewBox: "0 0 24 24", "aria-hidden": "true" });
  for (const d of (P[name] || P.spark).split(/(?=M)/)) s.appendChild(h("svg:path", { d }));
  return s;
}

export function brandMark(size = 28, id = "bm") {
  const s = h("svg:svg", { viewBox: "0 0 64 64", width: size, height: size, class: "brand-mark", "aria-hidden": "true" });
  const defs = h("svg:defs");
  const g = h("svg:linearGradient", { id: id + "g", x1: "0", y1: "0", x2: "1", y2: "1" });
  g.append(h("svg:stop", { offset: "0", "stop-color": "#6ea8ff" }), h("svg:stop", { offset: ".5", "stop-color": "#8b7bff" }),
    h("svg:stop", { offset: "1", "stop-color": "#3fd7c3" }));
  defs.appendChild(g); s.appendChild(defs);
  const pts = [[32, 6], [54, 19], [54, 45], [32, 58], [10, 45], [10, 19]];
  const hex = pts.map((p, i) => (i ? "L" : "M") + p.join(" ")).join("") + "Z";
  s.appendChild(h("svg:path", { d: hex, fill: "none", stroke: `url(#${id}g)`, "stroke-width": "3.2", "stroke-linejoin": "round" }));
  for (const p of pts) s.appendChild(h("svg:line", { x1: 32, y1: 32, x2: p[0], y2: p[1], stroke: `url(#${id}g)`, "stroke-width": "2", opacity: ".55" }));
  for (const p of pts) s.appendChild(h("svg:circle", { cx: p[0], cy: p[1], r: "4", fill: `url(#${id}g)` }));
  s.appendChild(h("svg:circle", { cx: 32, cy: 32, r: "6.5", fill: `url(#${id}g)` }));
  return s;
}

// Deterministic node avatar: a gradient hex tile seeded from the node id.
export function nodeAvatar(nodeId, size = 32) {
  let x = 0;
  for (const c of nodeId || "") x = (x * 31 + c.charCodeAt(0)) >>> 0;
  const h1 = x % 360, h2 = (h1 + 40 + (x >> 9) % 80) % 360;
  const d = h("div", { class: "avatar", style: { width: size + "px", height: size + "px",
    background: `linear-gradient(135deg, hsl(${h1} 70% 58%), hsl(${h2} 70% 46%))`, borderRadius: Math.round(size / 3.2) + "px" } });
  const s = h("svg:svg", { viewBox: "0 0 24 24", width: size * 0.55, height: size * 0.55 });
  s.appendChild(h("svg:path", { d: "M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9z", fill: "none", stroke: "rgba(255,255,255,.9)", "stroke-width": "2", "stroke-linejoin": "round" }));
  s.appendChild(h("svg:circle", { cx: 12, cy: 12, r: 2.6, fill: "rgba(255,255,255,.95)" }));
  d.appendChild(s);
  return d;
}

// ---------------------------------------------------------------- formatting
export const fmt = (n, d = 0) => n == null || Number.isNaN(n) ? "–"
  : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
export function compact(n) {
  if (n == null) return "–";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(n));
}
export const short = (id) => (id || "").slice(0, 6);
export function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

const B32 = "0123456789bcdefghjkmnpqrstuvwxyz";
export function decodeCell(cell) {
  let lat = [-90, 90], lon = [-180, 180], even = true;
  for (const c of cell || "") {
    const idx = B32.indexOf(c);
    for (let b = 4; b >= 0; b--) {
      const r = even ? lon : lat, mid = (r[0] + r[1]) / 2;
      if ((idx >> b) & 1) r[0] = mid; else r[1] = mid;
      even = !even;
    }
  }
  return [(lat[0] + lat[1]) / 2, (lon[0] + lon[1]) / 2];
}
export function regionName(cell) {
  if (!cell) return null;
  const [lat, lon] = decodeCell(cell);
  if (lat > 12 && lon < -30 && lon > -170) return "North America";
  if (lat <= 12 && lon < -30 && lon > -90) return "South America";
  if (lon >= -30 && lon < 60 && lat > 35) return "Europe";
  if (lon >= -20 && lon < 52 && lat <= 35) return "Africa";
  if (lat < -10 && lon >= 110) return "Oceania";
  if (lon >= 40) return "Asia";
  return "Atlantic";
}

// ---------------------------------------------------------------- overlays
let toastStack;
export function toast(message, kind = "") {
  toastStack ||= document.body.appendChild(h("div", { class: "toast-stack", role: "status", "aria-live": "polite" }));
  const t = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : "check", "sm"), message);
  toastStack.appendChild(t);
  setTimeout(() => { t.style.transition = "opacity .3s"; t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3200);
}

export async function copyText(text, label = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(label); }
  catch { toast("Couldn't access the clipboard", "bad"); }
}

export function modal({ title, subtitle, body, actions = [], wide = false, onClose, iconEl }) {
  const scrim = h("div", { class: "scrim" });
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const foot = h("div", { class: "modal-foot" }, actions.map((a) => h("button", {
    class: `btn ${a.kind || ""}`, onclick: async (e) => { if (a.onClick) { const r = await a.onClick(e); if (r === false) return; } close(); },
  }, a.icon ? icon(a.icon, "sm") : null, a.label)));
  const m = h("div", { class: `modal ${wide ? "wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div", { class: "modal-head" }, iconEl || null, h("div", {}, h("h3", { text: title }), subtitle ? h("p", { text: subtitle }) : null),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, icon("x"))),
    h("div", { class: "modal-body" }, body), actions.length ? foot : null);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  scrim.appendChild(m);
  document.body.appendChild(scrim);
  document.addEventListener("keydown", onKey);
  setTimeout(() => (m.querySelector("input, textarea, button.primary, button.glow") || m).focus?.(), 30);
  return { close, el: m };
}

let openPop = null;
export function popover(anchor, content, { align = "left", above = false, cls = "" } = {}) {
  closePopover();
  const pop = h("div", { class: `pop ${cls}`, role: "menu" }, content);  // size classes apply before measuring
  const r = anchor.getBoundingClientRect();
  if (above) pop.style.maxHeight = `${Math.max(200, r.top - 16)}px`;  // never cover the control it belongs to
  document.body.appendChild(pop);
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = align === "right" ? r.right - pw : r.left;
  left = Math.max(8, Math.min(left, innerWidth - pw - 8));
  let top = above ? r.top - ph - 8 : r.bottom + 8;
  if (!above && top + ph > innerHeight - 8) top = r.top - ph - 8;
  if (above && top < 8) top = r.bottom + 8 + ph < innerHeight ? r.bottom + 8 : 8;
  top = Math.max(8, Math.min(top, innerHeight - ph - 8));
  pop.style.left = left + "px"; pop.style.top = top + "px";
  const away = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) closePopover(); };
  const esc = (e) => { if (e.key === "Escape") closePopover(); };
  setTimeout(() => { document.addEventListener("mousedown", away); document.addEventListener("keydown", esc); });
  openPop = { pop, away, esc };
  return pop;
}
export function closePopover() {
  if (!openPop) return;
  openPop.pop.remove();
  document.removeEventListener("mousedown", openPop.away);
  document.removeEventListener("keydown", openPop.esc);
  openPop = null;
}

let tipEl;
export function tooltip(target, contentFn) {
  const show = () => {
    tipEl ||= document.body.appendChild(h("div", { class: "tip", role: "tooltip" }));
    tipEl.replaceChildren(contentFn());
    tipEl.style.display = "block";
    const r = target.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left + r.width / 2 - tipEl.offsetWidth / 2, innerWidth - tipEl.offsetWidth - 8));
    const top = r.top - tipEl.offsetHeight - 8 > 8 ? r.top - tipEl.offsetHeight - 8 : r.bottom + 8;
    tipEl.style.left = left + "px"; tipEl.style.top = top + "px";
  };
  const hide = () => { if (tipEl) tipEl.style.display = "none"; };
  target.addEventListener("mouseenter", show); target.addEventListener("mouseleave", hide);
  target.addEventListener("focus", show); target.addEventListener("blur", hide);
}

export function toggle(checked, onChange, label) {
  const input = h("input", { type: "checkbox", role: "switch", "aria-label": label });
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked, input));
  return h("label", { class: "switch" }, input, h("span"));
}

export function segmented(options, value, onChange) {
  const wrap = h("div", { class: "seg", role: "radiogroup" });
  for (const [val, label] of options) {
    const b = h("button", { class: val === value ? "on" : "", role: "radio", "aria-checked": String(val === value),
      onclick: () => { for (const x of wrap.children) { x.classList.remove("on"); x.setAttribute("aria-checked", "false"); }
        b.classList.add("on"); b.setAttribute("aria-checked", "true"); onChange(val); } }, label);
    wrap.appendChild(b);
  }
  return wrap;
}

// Plugin icon tile colored by category.
const CAT_COLORS = { Utilities: ["#6ea8ff", "#5b5cf0"], Mycel: ["#8b7bff", "#3fd7c3"], Productivity: ["#f5a35b", "#e05d8b"],
  Web: ["#3fb6ff", "#2a6fd6"], Files: ["#f2b544", "#e0742a"], Developer: ["#7c8a9e", "#3c4656"], Reasoning: ["#c58bff", "#6b4fe0"], Custom: ["#9aa0a6", "#5f6368"] };
export function pluginIcon(p, cls = "") {
  const [a, b] = CAT_COLORS[p.category] || CAT_COLORS.Custom;
  return h("div", { class: `picon ${cls}`, style: { background: `linear-gradient(135deg, ${a}, ${b})` } }, icon(p.icon, cls === "lg" ? "lg" : ""));
}

// Motion & delight: the parts of Mycel that make it feel alive. Everything respects
// prefers-reduced-motion and stops drawing when its element leaves the page.
import { h, icon } from "./ui.js";

export const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// Animate a number from its last shown value to `value`.
export function countUp(el, value, { digits = 0, suffix = "", ms = 900 } = {}) {
  const from = Number(el.dataset.v || 0), to = Number(value) || 0;
  el.dataset.v = String(to);
  const fmt = (v) => v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 }) + suffix;
  if (calm() || from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - k, 3);
    el.textContent = fmt(from + (to - from) * e);
    if (k < 1 && el.isConnected) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
const lastShown = new Map();
// A rolling number. With a `key`, a re-rendered counter continues from what was last on screen.
export function counter(value, opts = {}) {
  const el = h("span", { class: "num-roll" });
  if (opts.key) el.dataset.v = String(lastShown.get(opts.key) ?? 0);
  countUp(el, value, opts);
  if (opts.key) lastShown.set(opts.key, Number(value) || 0);
  return el;
}

// A burst of particles from an element (sharing switched on, achievement unlocked, …).
export function burst(from, { colors = ["#6ea8ff", "#8b7bff", "#3fd7c3", "#ff5fa8"], n = 26 } = {}) {
  if (calm() || !from) return;
  const r = from.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  for (let i = 0; i < n; i++) {
    const p = h("i", { class: "spark-p" });
    const a = Math.random() * Math.PI * 2, d = 40 + Math.random() * 90;
    Object.assign(p.style, { left: cx + "px", top: cy + "px", background: colors[i % colors.length],
      color: colors[i % colors.length], "--dx": Math.cos(a) * d + "px", "--dy": Math.sin(a) * d + "px", animationDelay: Math.random() * 80 + "ms" });
    document.body.appendChild(p);
    setTimeout(() => p.remove(), 1100);
  }
}

// Ambient living network: drifting nodes that link to near neighbours, packets riding the links.
export function meshField({ density = 0.00006, color = "139,123,255", packets = true } = {}) {
  const canvas = h("canvas", { class: "mesh-field", "aria-hidden": "true" });
  const ctx = canvas.getContext("2d");
  let nodes = [], w = 0, hgt = 0, pk = [];
  const resize = () => {
    const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    w = r.width; hgt = r.height;
    canvas.width = w * dpr; canvas.height = hgt * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.max(14, Math.min(90, Math.round(w * hgt * density)));
    nodes = Array.from({ length: n }, () => ({ x: Math.random() * w, y: Math.random() * hgt,
      vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25, r: 1.2 + Math.random() * 1.8 }));
  };
  const frame = () => {
    if (!canvas.isConnected) return;
    if (!w) resize();
    ctx.clearRect(0, 0, w, hgt);
    const L = 140;
    for (const n of nodes) { if (!calm()) { n.x += n.vx; n.y += n.vy; } if (n.x < 0 || n.x > w) n.vx *= -1; if (n.y < 0 || n.y > hgt) n.vy *= -1; }
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j], d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d < L) { ctx.strokeStyle = `rgba(${color},${(1 - d / L) * 0.18})`; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
    }
    for (const n of nodes) { ctx.fillStyle = `rgba(${color},.55)`; ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 7); ctx.fill(); }
    if (packets && !calm()) {
      if (pk.length < 6 && Math.random() < 0.05) {
        const a = nodes[(Math.random() * nodes.length) | 0], b = nodes.reduce((best, n) => n !== a && (!best || Math.hypot(n.x - a.x, n.y - a.y) < Math.hypot(best.x - a.x, best.y - a.y)) ? n : best, null);
        if (b && Math.hypot(a.x - b.x, a.y - b.y) < L) pk.push({ a, b, t: 0 });
      }
      pk = pk.filter((p) => (p.t += 0.02) < 1);
      for (const p of pk) {
        const x = p.a.x + (p.b.x - p.a.x) * p.t, y = p.a.y + (p.b.y - p.a.y) * p.t;
        const g = ctx.createRadialGradient(x, y, 0, x, y, 7); g.addColorStop(0, "rgba(63,215,195,.95)"); g.addColorStop(1, "rgba(63,215,195,0)");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.fill();
      }
    }
    requestAnimationFrame(frame);
  };
  new ResizeObserver(resize).observe(canvas);
  requestAnimationFrame(frame);
  return canvas;
}

// The reactor: your node's core. Ring colour, spin and orbiting sparks follow its state;
// `activity()` returns 0..1 (how hard your GPU is working for the mesh right now).
const PALETTE = {
  sharing: ["#3fd7c3", "#6ea8ff", "#8b7bff"], helping: ["#3fd7c3", "#6ea8ff", "#8b7bff"],
  swarm: ["#ff8a5b", "#ff5fa8", "#a78bff"], private: ["#8b7bff", "#6ea8ff", "#c7c4ff"],
  idle: ["#6ea8ff", "#4f6fae", "#8b7bff"], busy: ["#8b7bff", "#6ea8ff", "#3fd7c3"], off: ["#55555c", "#3a3a40", "#6a6a72"],
  offline: ["#55555c", "#3a3a40", "#6a6a72"], searching: ["#7d7d8c", "#4a4a56", "#9a9ab0"],
};
export function reactor({ size = 220, state = () => "idle", activity = () => 0 } = {}) {
  const canvas = h("canvas", { class: "reactor", width: size * 2, height: size * 2, style: { width: size + "px", height: size + "px" }, "aria-hidden": "true" });
  const ctx = canvas.getContext("2d");
  ctx.scale(2, 2);
  const c = size / 2;
  const sparks = Array.from({ length: 34 }, (_, i) => ({ a: Math.random() * 7, r: c * (0.52 + Math.random() * 0.36), s: 0.002 + Math.random() * 0.006, z: Math.random() }));
  let t = 0, act = 0;
  const frame = () => {
    if (!canvas.isConnected) return;
    const st = state(), pal = PALETTE[st] || PALETTE.idle;
    act += (Math.max(0, Math.min(1, activity())) - act) * 0.04;
    const live = st !== "off" && st !== "offline";
    t += calm() ? 0 : (0.6 + act * 3.4) * (live ? 1 : 0.15);
    ctx.clearRect(0, 0, size, size);
    // glow
    const glow = ctx.createRadialGradient(c, c, c * 0.1, c, c, c);
    glow.addColorStop(0, pal[0] + (live ? "55" : "22")); glow.addColorStop(0.5, pal[1] + "18"); glow.addColorStop(1, "transparent");
    ctx.fillStyle = glow; ctx.fillRect(0, 0, size, size);
    // rings
    for (let k = 0; k < 3; k++) {
      const rr = c * (0.48 + k * 0.14), seg = 5 + k * 3, rot = (t * 0.004 * (k % 2 ? -1 : 1) * (1 + k * 0.4));
      ctx.lineWidth = k === 0 ? 3 : 1.6; ctx.lineCap = "round";
      for (let i = 0; i < seg; i++) {
        const a0 = rot + (i / seg) * Math.PI * 2, a1 = a0 + (Math.PI * 2 / seg) * (0.55 - k * 0.1);
        ctx.strokeStyle = pal[(i + k) % 3] + (k === 0 ? "ee" : "88");
        ctx.beginPath(); ctx.arc(c, c, rr, a0, a1); ctx.stroke();
      }
    }
    // sparks
    for (const s of sparks) {
      s.a += calm() ? 0 : s.s * (1 + act * 5) * (live ? 1 : 0.1);
      const x = c + Math.cos(s.a) * s.r, y = c + Math.sin(s.a) * s.r * 0.94;
      ctx.fillStyle = pal[(s.z * 3) | 0] + (live ? "cc" : "44");
      ctx.beginPath(); ctx.arc(x, y, 0.8 + s.z * 1.6, 0, 7); ctx.fill();
    }
    // core
    const pulse = 1 + Math.sin(t * 0.03) * (0.04 + act * 0.08);
    const core = ctx.createRadialGradient(c, c, 0, c, c, c * 0.3 * pulse);
    core.addColorStop(0, "#ffffff" + (live ? "ff" : "55")); core.addColorStop(0.35, pal[0] + (live ? "ff" : "66")); core.addColorStop(1, pal[2] + "00");
    ctx.fillStyle = core; ctx.beginPath(); ctx.arc(c, c, c * 0.32 * pulse, 0, 7); ctx.fill();
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return canvas;
}

// ---------------------------------------------------------------- progression
export const LEVELS = [[0, "Seedling"], [100, "Spark"], [1000, "Sprout"], [5000, "Root"], [25000, "Mycelium"],
  [100000, "Grove"], [500000, "Forest"], [2000000, "Biome"]];
export function level(tokens) {
  let i = 0;
  while (i + 1 < LEVELS.length && tokens >= LEVELS[i + 1][0]) i++;
  const [lo, name] = LEVELS[i], hi = LEVELS[i + 1]?.[0];
  return { n: i + 1, name, lo, hi, next: LEVELS[i + 1]?.[1], frac: hi ? (tokens - lo) / (hi - lo) : 1 };
}

const SEEN = "mycel.achievements";
export const flags = () => JSON.parse(localStorage.getItem("mycel.flags") || "{}");
export function flag(k) { const f = flags(); if (!f[k]) { f[k] = Date.now(); localStorage.setItem("mycel.flags", JSON.stringify(f)); } }

// Unlock toast, game-style. Called with the achievements now earned; announces the new ones once.
export function announce(achs) {
  const seen = new Set(JSON.parse(localStorage.getItem(SEEN) || "[]"));
  const first = !localStorage.getItem(SEEN);
  const fresh = achs.filter((a) => a.done && !seen.has(a.id));
  achs.filter((a) => a.done).forEach((a) => seen.add(a.id));
  localStorage.setItem(SEEN, JSON.stringify([...seen]));
  if (first) return;   // don't flood a returning user with old unlocks
  fresh.forEach((a, i) => setTimeout(() => {
    const el = h("div", { class: "unlock", role: "status" }, h("span", { class: "u-ic" }, icon(a.icon)),
      h("div", {}, h("div", { class: "u-k" }, "Achievement unlocked"), h("div", { class: "u-t" }, a.title)));
    document.body.appendChild(el);
    burst(el.querySelector(".u-ic"), { n: 34 });
    setTimeout(() => el.classList.add("out"), 3600);
    setTimeout(() => el.remove(), 4200);
  }, i * 900));
}

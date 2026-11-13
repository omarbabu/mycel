// GPU-drawn visuals: an aurora behind the whole app, the plasma-orb reactor, and a dotted 3D globe.
// Hand-written WebGL / canvas, no libraries. Each falls back gracefully, renders at modest
// resolution, pauses when the tab is hidden, and holds still for reduced motion.
import { h } from "./ui.js";
import { calm } from "./fx.js";

const STATE_COLORS = {   // [deep, mid, bright] per node state
  sharing: [[0.05, 0.32, 0.30], [0.25, 0.84, 0.76], [0.43, 0.66, 1.0]],
  helping: [[0.05, 0.32, 0.30], [0.25, 0.84, 0.76], [0.43, 0.66, 1.0]],
  swarm: [[0.42, 0.10, 0.30], [1.0, 0.37, 0.66], [1.0, 0.54, 0.36]],
  private: [[0.20, 0.16, 0.48], [0.55, 0.48, 1.0], [0.43, 0.66, 1.0]],
  idle: [[0.12, 0.20, 0.42], [0.43, 0.66, 1.0], [0.55, 0.48, 1.0]],
  busy: [[0.20, 0.16, 0.48], [0.55, 0.48, 1.0], [0.25, 0.84, 0.76]],
  off: [[0.12, 0.12, 0.14], [0.32, 0.32, 0.36], [0.45, 0.45, 0.5]],
  offline: [[0.12, 0.12, 0.14], [0.32, 0.32, 0.36], [0.45, 0.45, 0.5]],
  searching: [[0.16, 0.16, 0.20], [0.42, 0.42, 0.50], [0.58, 0.58, 0.68]],
};
const lerp3 = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);

function program(gl, frag) {
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
  const p = gl.createProgram();
  gl.attachShader(p, sh(gl.VERTEX_SHADER, "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}"));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, frag));
  gl.linkProgram(p);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(p, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  gl.useProgram(p);
  return (name) => gl.getUniformLocation(p, name);
}

// A shader canvas that animates while attached and visible. `draw(gl, u, t, w, h)` sets uniforms.
function shaderCanvas(cls, frag, scale, draw, fallback) {
  const canvas = h("canvas", { class: cls, "aria-hidden": "true" });
  const gl = canvas.getContext("webgl", { premultipliedAlpha: false, alpha: true, antialias: false });
  if (!gl) return fallback ? fallback() : canvas;
  let u;
  try { u = program(gl, frag); } catch (e) { console.warn(e); return fallback ? fallback() : canvas; }
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  const t0 = performance.now();
  let last = 0;
  const frame = (now) => {
    if (!canvas.isConnected) { if (canvas.dataset.seen) return; requestAnimationFrame(frame); return; }
    canvas.dataset.seen = "1";
    if (!document.hidden && now - last > 30) {   // ~30 fps is plenty for ambient motion
      last = now;
      const r = canvas.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1) * scale;
      const w = Math.max(2, Math.round(r.width * dpr)), hh = Math.max(2, Math.round(r.height * dpr));
      if (canvas.width !== w || canvas.height !== hh) { canvas.width = w; canvas.height = hh; gl.viewport(0, 0, w, hh); }
      draw(gl, u, calm() ? 12 : (now - t0) / 1000, w, hh);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return canvas;
}

// ---------------------------------------------------------------- aurora backdrop
const AURORA = `precision mediump float;uniform float t;uniform vec2 r;uniform vec3 a,b,c;uniform float k;
void main(){vec2 uv=gl_FragCoord.xy/r;vec2 p=uv*vec2(r.x/r.y,1.)*1.6;float v=0.;
for(int i=0;i<5;i++){float f=float(i);p+=vec2(sin(t*.05+f*1.7+p.y*1.3),cos(t*.04+f*2.1+p.x*1.1))*.32;v+=sin(p.x*1.1+p.y*.7+t*.07+f)*.5+.5;}
v/=5.;vec3 col=mix(a,b,smoothstep(.25,.8,v));col=mix(col,c,smoothstep(.6,.95,v)*.7);
float top=smoothstep(-.1,1.,uv.y);float vig=smoothstep(1.25,.15,length((uv-vec2(.55,.8))*vec2(1.,1.4)));
gl_FragColor=vec4(col,.55*k*vig*top);}`;
export function aurora(getState) {
  let cur = STATE_COLORS.idle;
  return shaderCanvas("aurora", AURORA, 0.35, (gl, u, t, w, hh) => {
    const want = STATE_COLORS[getState()] || STATE_COLORS.idle;
    cur = cur.map((c, i) => lerp3(c, want[i], 0.03));
    gl.uniform1f(u("t"), t); gl.uniform2f(u("r"), w, hh);
    gl.uniform3fv(u("a"), cur[0]); gl.uniform3fv(u("b"), cur[1]); gl.uniform3fv(u("c"), cur[2]);
    gl.uniform1f(u("k"), document.documentElement.dataset.theme === "light" || matchMedia("(prefers-color-scheme: light)").matches && !document.documentElement.dataset.theme ? 0.35 : 1);
  });
}

// ---------------------------------------------------------------- plasma orb (the reactor)
const ORB = `precision highp float;uniform float t,act;uniform vec2 r;uniform vec3 a,b,c;
void main(){vec2 uv=(gl_FragCoord.xy-.5*r)/(.5*r.y);float d=length(uv);float R=.56;
vec3 n=vec3(uv,sqrt(max(0.,R*R-d*d)))/R;vec3 q=n*2.6;float s=0.;
for(int i=0;i<5;i++){q+=vec3(sin(q.y*1.1+t*(.35+act*1.2)),sin(q.z*1.3+t*.27),sin(q.x*.9-t*(.42+act)))*.55;s+=sin(q.x*1.7+q.y*1.3+q.z*1.1);}
s=s*.1+.5;float inside=smoothstep(R,R-.012,d);
vec3 col=mix(a,b,smoothstep(.15,.85,s));col=mix(col,c,pow(clamp(s,0.,1.),3.)*.9);
float fres=pow(1.-n.z,2.2);float spec=pow(max(0.,dot(n,normalize(vec3(-.45,.55,.7)))),28.);
vec3 sph=col*(.25+1.1*s)+fres*b*1.5+vec3(spec)*.55;
float glow=exp(-5.5*max(0.,d-R))*(.35+act*.9);
float ring=0.;float ang=atan(uv.y,uv.x);
for(int k=0;k<3;k++){float fk=float(k);float rr=R+.11+.085*fk;float dir=mod(fk,2.)*2.-1.;
float seg=smoothstep(.45,.55,fract(ang/6.2831*(3.+fk*2.)+t*(.05+act*.35)*dir));ring+=smoothstep(.007,0.,abs(d-rr))*seg*(.8-.2*fk);}
float spark=0.;for(int k=0;k<14;k++){float fk=float(k);float aa=fk*2.39996+t*(.12+act*.7)*(1.+mod(fk,3.)*.3);
float rr=R+.07+.22*fract(fk*.618);vec2 pp=vec2(cos(aa),sin(aa))*rr;spark+=.0009/max(.0004,dot(uv-pp,uv-pp));}
vec3 outc=sph*inside+(b*glow+mix(a,c,.5)*ring*1.3+c*spark*.35)*(1.-inside);
float alpha=max(inside,clamp(glow*.9+ring+spark*.3,0.,1.));gl_FragColor=vec4(outc,alpha);}`;
export function plasmaOrb({ size = 230, state = () => "idle", activity = () => 0, fallback } = {}) {
  let cur = STATE_COLORS.idle, act = 0;
  const c = shaderCanvas("orb-gl", ORB, 1, (gl, u, t, w, hh) => {
    const want = STATE_COLORS[state()] || STATE_COLORS.idle;
    cur = cur.map((x, i) => lerp3(x, want[i], 0.05));
    act += (Math.max(0, Math.min(1, activity())) - act) * 0.05;
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform1f(u("t"), t); gl.uniform1f(u("act"), act); gl.uniform2f(u("r"), w, hh);
    gl.uniform3fv(u("a"), cur[0]); gl.uniform3fv(u("b"), cur[1]); gl.uniform3fv(u("c"), cur[2]);
  }, fallback);
  c.style.width = c.style.height = size + "px";
  return c;
}

// ---------------------------------------------------------------- dotted globe
let LAND_DOTS = null;
function landDots() {
  if (LAND_DOTS) return LAND_DOTS;
  const rings = (window.LAND_RINGS || []).map((r) => { const pts = []; for (let i = 0; i < r.length; i += 2) pts.push([r[i], r[i + 1]]); return pts; });
  const boxes = rings.map((r) => { let a = 999, b = -999, c = 999, d = -999; for (const [x, y] of r) { a = Math.min(a, x); b = Math.max(b, x); c = Math.min(c, y); d = Math.max(d, y); } return [a, b, c, d]; });
  const inside = (lon, lat) => {
    for (let k = 0; k < rings.length; k++) {
      const [a, b, c, d] = boxes[k];
      if (lon < a || lon > b || lat < c || lat > d) continue;
      let ins = false; const r = rings[k];
      for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
        const [xi, yi] = r[i], [xj, yj] = r[j];
        if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) ins = !ins;
      }
      if (ins) return true;
    }
    return false;
  };
  const out = [];
  for (let lat = -58; lat <= 82; lat += 2.4) {
    const step = 2.4 / Math.max(0.2, Math.cos((lat * Math.PI) / 180));
    for (let lon = -180; lon < 180; lon += step) if (inside(lon, lat)) out.push([lon, lat]);
  }
  return (LAND_DOTS = out);
}
const rad = (d) => (d * Math.PI) / 180;
function vec(lat, lon) { const la = rad(lat), lo = rad(lon); return [Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo)]; }

// points: () => [{lat, lon, color, self, label}], links: () => [[a, b, color]] (indexes into points)
export function globe({ points, links, height = 380 }) {
  const canvas = h("canvas", { class: "globe", style: { height: height + "px" }, role: "img", "aria-label": "Globe of mesh members who share a coarse location (drag to rotate)" });
  const ctx = canvas.getContext("2d");
  let rotLon = -10, rotLat = 18, drag = null, vel = 0.06, t = 0;
  canvas.addEventListener("pointerdown", (e) => { drag = [e.clientX, e.clientY, rotLon, rotLat]; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", (e) => { if (!drag) return; rotLon = drag[2] - (e.clientX - drag[0]) * 0.35; rotLat = Math.max(-60, Math.min(60, drag[3] + (e.clientY - drag[1]) * 0.3)); });
  canvas.addEventListener("pointerup", () => { drag = null; });
  const project = (v, cx, cy, R) => {   // rotate by -rotLon around y, then by rotLat around x
    const lo = rad(rotLon), la = rad(rotLat);
    let [x, y, z] = v;
    [x, z] = [x * Math.cos(lo) + z * Math.sin(lo), -x * Math.sin(lo) + z * Math.cos(lo)];
    [y, z] = [y * Math.cos(la) - z * Math.sin(la), y * Math.sin(la) + z * Math.cos(la)];
    return [cx - x * R, cy - y * R, z];   // east to the right
  };
  const frame = () => {
    if (!canvas.isConnected) { if (canvas.dataset.seen) return; requestAnimationFrame(frame); return; }
    canvas.dataset.seen = "1";
    const r = canvas.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
    if (canvas.width !== Math.round(r.width * dpr)) { canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = r.width, H = r.height, R = Math.min(W, H) * 0.44, cx = W / 2, cy = H / 2;
    if (!drag && !calm()) rotLon -= vel;
    t += calm() ? 0 : 0.016;
    ctx.clearRect(0, 0, W, H);
    const halo = ctx.createRadialGradient(cx, cy, R * 0.85, cx, cy, R * 1.35);
    halo.addColorStop(0, "rgba(110,140,255,.22)"); halo.addColorStop(1, "rgba(110,140,255,0)");
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(cx, cy, R * 1.35, 0, 7); ctx.fill();
    const body = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.4, R * 0.1, cx, cy, R);
    body.addColorStop(0, "rgba(40,44,70,.95)"); body.addColorStop(1, "rgba(10,11,18,.98)");
    ctx.fillStyle = body; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fill();
    for (const [lon, lat] of landDots()) {
      const [x, y, z] = project(vec(lat, lon), cx, cy, R);
      if (z <= 0) continue;
      ctx.fillStyle = `rgba(150,160,220,${0.18 + z * 0.55})`;
      ctx.fillRect(x - 0.9, y - 0.9, 1.8, 1.8);
    }
    const pts = points();
    const P = pts.map((p) => project(vec(p.lat, p.lon), cx, cy, R));
    for (const [ia, ib, color] of links()) {
      const a = vec(pts[ia].lat, pts[ia].lon), b = vec(pts[ib].lat, pts[ib].lon);
      const dot = Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])), om = Math.acos(dot);
      if (om < 1e-3) continue;
      const seg = [];
      for (let i = 0; i <= 32; i++) {
        const f = i / 32, s1 = Math.sin((1 - f) * om) / Math.sin(om), s2 = Math.sin(f * om) / Math.sin(om);
        const lift = 1 + Math.sin(f * Math.PI) * Math.min(0.28, om * 0.22);
        seg.push(project([(a[0] * s1 + b[0] * s2) * lift, (a[1] * s1 + b[1] * s2) * lift, (a[2] * s1 + b[2] * s2) * lift], cx, cy, R));
      }
      ctx.lineWidth = 1.4;
      for (let i = 1; i < seg.length; i++) {
        if (seg[i][2] < -0.05 && seg[i - 1][2] < -0.05) continue;
        ctx.strokeStyle = color + (seg[i][2] > 0 ? "aa" : "33");
        ctx.beginPath(); ctx.moveTo(seg[i - 1][0], seg[i - 1][1]); ctx.lineTo(seg[i][0], seg[i][1]); ctx.stroke();
      }
      const k = Math.floor(((t * 0.6 + ia * 0.37 + ib * 0.11) % 1) * 32), q = seg[k];
      if (q && q[2] > 0) { const g = ctx.createRadialGradient(q[0], q[1], 0, q[0], q[1], 6); g.addColorStop(0, "#ffffff"); g.addColorStop(1, color + "00");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(q[0], q[1], 6, 0, 7); ctx.fill(); }
    }
    pts.forEach((p, i) => {
      const [x, y, z] = P[i];
      if (z <= 0) return;
      const pulse = 1 + 0.35 * Math.sin(t * 3 + i);
      const g = ctx.createRadialGradient(x, y, 0, x, y, 10 * pulse);
      g.addColorStop(0, p.color); g.addColorStop(1, p.color + "00");
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 10 * pulse, 0, 7); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(x, y, p.self ? 3.2 : 2.2, 0, 7); ctx.fill();
      if (p.self) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.3; ctx.beginPath(); ctx.arc(x, y, 7 + 2 * Math.sin(t * 2), 0, 7); ctx.stroke(); }
    });
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return canvas;
}

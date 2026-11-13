// Thin client for the node's loopback API.
async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const msg = data?.error?.message || data?.error || `Request failed (${r.status})`;
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return data;
}
export const api = {
  get: (p) => request("GET", p),
  post: (p, b = {}) => request("POST", p, b),
  put: (p, b = {}) => request("PUT", p, b),
  del: (p) => request("DELETE", p),
};

// Stream NDJSON events from /api/chat. Calls onEvent for each; resolves when the stream ends.
export async function streamChat(body, onEvent, signal) {
  const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  if (!r.ok) {
    let msg = `Request failed (${r.status})`;
    try { msg = (await r.json()).error || msg; } catch { /* empty */ }
    throw new Error(msg);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  if (buf.trim()) onEvent(JSON.parse(buf));
}

// Small, safe Markdown renderer. All source text is HTML-escaped first; only tags generated
// here reach innerHTML, and link targets are restricted to http(s)/mailto.
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const KW = /\b(def|class|return|if|elif|else|for|while|in|import|from|as|with|try|except|finally|raise|lambda|yield|await|async|const|let|var|function|new|this|export|default|interface|type|struct|fn|pub|impl|match|use|mod|package|func|go|defer|select|case|switch|break|continue|true|false|null|None|True|False|nil|and|or|not|is)\b/g;

function highlight(code) {
  // code is already escaped. Tokenize comments and strings first so keywords inside them are left alone.
  const out = [];
  const re = /(#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(&quot;(?:[^&]|&(?!quot;))*?&quot;|&#39;(?:[^&]|&(?!#39;))*?&#39;|`[^`]*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)(?=\()/g;
  let last = 0, m;
  while ((m = re.exec(code))) {
    out.push(code.slice(last, m.index).replace(KW, '<span class="tok-k">$1</span>'));
    if (m[1]) out.push(`<span class="tok-c">${m[1]}</span>`);
    else if (m[2]) out.push(`<span class="tok-s">${m[2]}</span>`);
    else if (m[3]) out.push(`<span class="tok-n">${m[3]}</span>`);
    else if (m[4]) out.push(KW.test(m[4]) ? m[4].replace(KW, '<span class="tok-k">$1</span>') : `<span class="tok-f">${m[4]}</span>`);
    KW.lastIndex = 0;
    last = re.lastIndex;
  }
  out.push(code.slice(last).replace(KW, '<span class="tok-k">$1</span>'));
  return out.join("");
}

function inline(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (all, text, url) => {
    const u = url.replace(/&amp;/g, "&");
    if (!/^(https?:\/\/|mailto:)/i.test(u)) return text;
    return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer nofollow">${text}</a>`;
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>").replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

export function renderMarkdown(src, { streaming = false } = {}) {
  const lines = esc(src.replace(/\r\n?/g, "\n")).split("\n");
  const html = [];
  let i = 0;
  const para = [];
  const flush = () => { if (para.length) { html.push(`<p>${inline(para.join("<br>"))}</p>`); para.length = 0; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w+#.-]*)\s*$/);
    if (fence) {
      flush();
      const lang = fence[1] || "";
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      html.push(`<div class="codeblock"><div class="bar"><span>${lang || "code"}</span><button class="btn ghost sm" data-copy>Copy</button></div>` +
        `<pre><code>${highlight(body.join("\n"))}</code></pre></div>`);
      continue;
    }
    if (/^\s*$/.test(line)) { flush(); i++; continue; }
    const hd = line.match(/^(#{1,4})\s+(.*)$/);
    if (hd) { flush(); const n = Math.min(3, hd[1].length); html.push(`<h${n}>${inline(hd[2])}</h${n}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flush(); html.push("<hr>"); i++; continue; }
    if (/^&gt;\s?/.test(line)) {
      flush();
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^&gt;\s?/, ""));
      html.push(`<blockquote>${renderMarkdown(q.join("\n").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&"))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      html.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>` +
        rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</tbody></table>");
      continue;
    }
    const li = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
        if (!m || /\d/.test(m[1]) !== ordered) break;
        items.push(inline(m[2]));
        i++;
      }
      html.push(`<${ordered ? "ol" : "ul"}>${items.map((x) => `<li>${x}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  let out = html.join("");
  if (streaming) {
    const caret = '<span class="caret"></span>';
    const m = out.match(/(<\/li><\/[ou]l>|<\/p>)$/);
    out = m ? out.slice(0, -m[0].length) + caret + m[0] : out + caret;
  }
  return out;
}

import { api } from "./api.js";
import { state, on } from "./state.js";
import { h, icon, copyText, toast, fmt } from "./ui.js";

function snippets(base, model) {
  return {
    "cURL": ["bash", `curl ${base}/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello from my agent"}]}'`],
    "Python": ["python", `from openai import OpenAI

client = OpenAI(base_url="${base}", api_key="local")  # any value: local-only

reply = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Hello from my agent"}],
)
print(reply.choices[0].message.content)`],
    "TypeScript": ["ts", `import OpenAI from "openai";

const client = new OpenAI({ baseURL: "${base}", apiKey: "local" });

const reply = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "Hello from my agent" }],
});
console.log(reply.choices[0].message.content);`],
    "LangChain": ["python", `from langchain_openai import ChatOpenAI

llm = ChatOpenAI(base_url="${base}", api_key="local", model="${model}")
print(llm.invoke("Hello from my agent").content)`],
    "Vercel AI SDK": ["ts", `import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { generateText } from "ai";

const mesh = createOpenAICompatible({ name: "mycel", baseURL: "${base}" });
const { text } = await generateText({ model: mesh("${model}"), prompt: "Hello from my agent" });`],
    "OpenAI Agents SDK": ["python", `from agents import Agent, Runner, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI

set_tracing_disabled(True)  # keep traces on your machine
mesh = AsyncOpenAI(base_url="${base}", api_key="local")
agent = Agent(name="Helper", instructions="Be concise.",
              model=OpenAIChatCompletionsModel(model="${model}", openai_client=mesh))
print(Runner.run_sync(agent, "Hello from my agent").final_output)`],
    "Aider / Continue / Cline": ["text", `Choose the "OpenAI-compatible" provider and set:

  Base URL:  ${base}
  API key:   local          (any value — it never leaves this machine)
  Model:     ${model}

Aider:  aider --openai-api-base ${base} --openai-api-key local --model openai/${model}`],
  };
}

export function render(container) {
  const offs = [];
  let tab = "Python";
  const base = `${location.origin}/v1`;
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const code = h("pre", { style: { margin: 0, padding: "16px 18px", font: "13px/1.65 var(--mono)", overflowX: "auto" } });
  const modelsList = h("div", { style: { display: "flex", flexWrap: "wrap", gap: "8px" } });
  const status = h("span", { class: "hint" });

  container.append(h("div", { class: "view" }, h("div", { class: "page", style: { maxWidth: "940px" } },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Connect an agent"),
      h("p", {}, "Point any OpenAI-compatible app, agent or framework at your node. It routes across the mesh for you."))),
    h("div", { class: "endpoint" }, icon("plug", "lg"),
      h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { class: "hint" }, "Base URL"), h("div", { class: "url" }, base)),
      h("button", { class: "btn", onclick: () => copyText(base, "Base URL copied") }, icon("copy", "sm"), "Copy"),
      h("button", { class: "btn primary", onclick: test }, icon("bolt", "sm"), "Test")),
    h("div", { style: { margin: "8px 2px 22px" } }, status),
    h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", marginBottom: "18px" } },
      feature("lock", "Local-only endpoint", "Listens on 127.0.0.1. Nothing outside this computer can reach it, so no API key is needed."),
      feature("puzzle", "Tool calling", "Pass OpenAI-style tools; tool calls come back to your agent to run. The mesh never executes them."),
      feature("shieldCheck", "Verified replies", "Every response carries the serving node's id and a signed receipt in the extra mesh field.")),
    h("div", { class: "card", style: { padding: 0, overflow: "hidden" } }, h("div", { style: { padding: "0 10px" } }, tabs),
      h("div", { style: { position: "relative" } }, code,
        h("button", { class: "btn sm", style: { position: "absolute", top: "10px", right: "10px" }, onclick: () => copyText(code.textContent, "Snippet copied") }, icon("copy", "sm"), "Copy"))),
    h("div", { class: "card", style: { marginTop: "14px" } }, h("h2", {}, "Model names"),
      h("p", {}, "Use auto to always get the best model with free capacity, or pin a specific one."), modelsList))));

  function feature(ic, t, d) {
    return h("div", { class: "card" }, h("div", { style: { display: "flex", gap: "10px", alignItems: "center", marginBottom: "6px" } },
      h("span", { class: "ic-tile" }, icon(ic, "sm")), h("b", {}, t)), h("div", { class: "hint", style: { fontSize: "13px" } }, d));
  }
  function draw() {
    const model = "auto";
    const s = snippets(base, model);
    tabs.replaceChildren(...Object.keys(s).map((k) => h("button", { class: k === tab ? "on" : "", role: "tab", "aria-selected": String(k === tab), onclick: () => { tab = k; draw(); } }, k)));
    code.textContent = s[tab][1];
    modelsList.replaceChildren(h("button", { class: "chip", onclick: () => copyText("auto", "Copied model name") }, icon("spark", "sm"), h("span", { class: "mono" }, "auto")),
      ...state.models.map((m) => h("button", { class: "chip", onclick: () => copyText(m.id, "Copied model name") },
        h("span", { class: "mono" }, m.id), h("span", { class: "muted" }, `${m.tier} · ${m.nodes}`))));
  }
  async function test() {
    status.textContent = "Testing…";
    try {
      const t0 = performance.now();
      const r = await fetch("/v1/chat/completions", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "auto", messages: [{ role: "user", content: "Say hello" }], max_tokens: 24 }) });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error?.message || r.status);
      status.replaceChildren(h("span", { style: { color: "var(--good)" } }, "✓ Working. "),
        `${data.model} answered via ${data.mesh.local ? "your own GPU" : "node " + data.mesh.served_by.slice(0, 6)} in ${fmt(performance.now() - t0)} ms.`);
    } catch (e) { status.textContent = ""; toast(`Test failed: ${e.message}`, "bad"); }
  }
  offs.push(on("mesh", draw));
  draw();
  return () => offs.forEach((f) => f());
}

# Mycel launch posts (drafts)

> Placeholders: `[LINK]` = repo or download page, `[VIDEO]` = the launch video.
> Every claim below matches what the code does today. Update the "what's not done" list
> before posting if that changes.

---

## Hacker News — Show HN

**Title** (≤ 80 chars)

```
Show HN: Mycel – pool spare GPUs into one founderless mesh for AI chat
```

**Text**

I've been thinking about what happens when capable AI becomes as basic as internet access, and
it still only runs in a handful of data centers. Mesh networks got people online during
blackouts. Mycel is an attempt at the same idea for inference.

You run a node. If your machine has a GPU, it serves an open-weight model (Qwen3, gpt-oss,
Gemma 3, Llama) and donates a hard-capped share of its time to the mesh. If it doesn't, you
just use what other members share. Everyone gets a chat app, plugins, and an OpenAI-compatible
endpoint on 127.0.0.1 for their own agents.

How it works:

- **Founderless membership.** There's no account and no central server. Each node is an Ed25519
  key, and joining the global mesh means presenting a proof-of-work over that key (~1 s of CPU,
  once). That makes mass-producing fake identities expensive without anyone in charge. Private
  invite-only meshes exist too, using certificate chains with founder revocation.
- **Gossip, not a coordinator.** Nodes exchange signed, constant-size adverts (capabilities,
  models, remaining budget) and keep a bounded view of peers. In a 150-node loopback test,
  views converged in ~9 s at ~0.6% CPU per node with constant ~20 KB messages.
- **Routing on measured behavior.** Requests go to the node with the model, budget and best
  *observed* speed and reliability. Self-reported hardware is discounted until measured.
  There's failover if a node drops mid-request.
- **Signed receipts.** The serving node signs a hash of exactly what it returned. The UI shows
  who answered and a Verified badge. This proves provenance, not correctness.
- **Hard limits for donors.** You set a max share of GPU time, concurrency, daily cap and
  per-member rate. Work is admitted against a reserved budget and killed at a deadline, so the
  mesh can't exceed what you allowed. Your own chats don't count against it.
- **One model name = one file.** Every node serving `qwen3-8b` runs the identical GGUF, so the
  model you chat with locally is the model you serve to others. The app tells you which models
  fit your GPU and estimates their speed from your measured memory bandwidth.
- **Plugins run on your machine, never on the node that asked for them.** There are built-ins
  plus any MCP server. Side-effecting tools require approval per call, and MCP servers start
  with a scrubbed environment (no inherited API keys or cloud credentials).

What's not done (and matters):

- **Internet transport.** Today nodes talk over loopback or a private overlay you set up
  (WireGuard/Tailscale). NAT traversal and relays come next.
- **Prompt privacy.** The node that answers sees your messages. "Prefer my own GPU" keeps
  prompts local when you can serve the model. Real fixes (TEEs, split inference) are research.
- **Sybil resistance and fairness.** PoW raises the cost of attack but doesn't stop a
  determined one. Contribution credits (receipts → priority) come next.
- **Security review.** The crypto primitives are standard (Ed25519 via `cryptography`), but
  the protocol around them hasn't been independently reviewed.

It's Python (aiohttp) plus a dependency-free web app, with llama.cpp doing the inference.
I'd especially love feedback on the membership model for a founderless network, and on what
would make you leave a node running.

[LINK] · [VIDEO]

---

## LinkedIn

**Intelligence should be a commons, not a utility bill.**

When the internet goes down, mesh networks keep people connected: neighbors relaying for
neighbors, no company in the middle.

AI is becoming that kind of essential. But capable models still run in a few data centers,
and access depends on who can pay for them.

So I built **Mycel** — named after mycelium, the underground network that lets a forest share
resources with no center.

🌐 **One global mesh, owned by no one.** Install it and tap Join. No account.
⚡ **Your GPU, your rules.** Share 10%, 25% or 50% of your GPU, with a hard limit the mesh can't exceed.
🛡️ **Every answer is signed** by the machine that wrote it, so you always know who answered.
🧩 **Plugins and agents.** A marketplace of tools that run on your own device, plus an OpenAI-compatible endpoint for any agent you use.
🔒 **Private by default.** Chats stay on your device. Location sharing is opt-in and always coarse.

The more people join, the faster it gets for everyone. Someone with a gaming PC in Lagos and
someone with a MacBook in Toronto end up sharing the same pool of intelligence.

It's early. It's an open beta, and honest about its limits. If you've got a GPU sitting idle
tonight, I'd love for you to try it and tell me what's missing.

👉 [LINK]
🎬 [VIDEO]

#AI #OpenSource #DecentralizedAI #LLM

---

## Short versions

**X / Bluesky (≤ 280)**
Mycel: pool spare GPUs into one founderless mesh. Tap Join, share a hard-capped slice of your
GPU, and chat with open models served by the community — every reply signed by the machine
that wrote it. OpenAI-compatible endpoint for your agents. Open beta → [LINK]

**One-liner**
Mycel turns idle GPUs into a shared, founderless AI commons.

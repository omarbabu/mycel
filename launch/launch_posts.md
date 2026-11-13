# Mycel launch posts (drafts)

> Placeholders: `[LINK]` = repo or download page, `[VIDEO]` = `launch/video/mycel-launch.mp4` (81 s; app footage
> is labeled "Demo network · simulated members").
> Every claim below matches the code and measurements as of 2026-11-04. **Before posting:** get Amazon
> outside-activity / open-source approval, stand up public relays, and re-check the "not done" list.
> Until a GPU stage engine ships, don't claim real 70B swarms. Claim what's measured.

---

## Hacker News — Show HN

**Title** (≤ 80 chars)

```
Show HN: Mycel – run models too big for your GPU across other people's GPUs
```

**Text**

The best open models need ~48 GB of GPU memory, and most people's GPUs have 8. Mycel is a
founderless mesh where members share spare GPU time. Its main trick: models that fit nobody's
card run as a **swarm**. Each member holds a slice of the layers, and your request flows through
them in order.

What makes it different from "a model hosted on someone else's GPU":

- **Your words stay on your device.** Your node tokenizes, embeds and samples locally. Stages only
  ever see activations. (These can in principle be inverted, so this isn't cryptographic privacy.
  But no node ever receives text or token ids.)
- **Nobody assigns layers.** Every host gossips which slice it holds, and each one places itself on
  the thinnest stretch of the model. A host may only move if its own layers stay covered, and among
  overlapping hosts only the lowest-id movable one goes first. Placement fills gaps as members join,
  adds replicas as more arrive, and never opens a hole when someone moves.
- **Routing is a shortest path over layer boundaries**, priced by measured RTT plus compute. During
  decoding, an "express lane" passes activations stage → stage (N+1 network legs per token instead
  of 2N). Speculative decoding drafts tokens on your device and is exact at temperature 0.
- **It heals mid-reply.** If a stage disappears, its layers are re-planned onto replicas and the
  context is replayed. **It checks the math:** one slice per reply is recomputed by a random
  independent replica, and a lying node is caught and dropped (tested).
- **You can see the path every reply took:** which member computed which layers, compute vs.
  network time, failovers, cross-checks, and who saw what.

Plumbing: nodes open no inbound ports. They keep outbound WebSockets to a few relays, and every
message is sealed end to end with HPKE (RFC 9180, via `cryptography`), so relays forward
ciphertext. Joining is a ~1 s proof-of-work over your node key, with no accounts. Members are
private by default: device model, location and activity are off unless you share them, and behind
a relay your IP isn't in what you broadcast.

What's measured (all on one laptop; nothing was exposed to the network):

- Qwen3 1.7B split across 4 nodes through a relay: **token-identical to llama.cpp**, 10 tok/s on a
  NumPy reference engine. The Q8_0, Q4_K and Q6_K decoders and the BPE tokenizer also match
  llama.cpp exactly.
- A simulated Llama 3.3 70B over 8 nodes with 8 GB GPUs at 30 ms RTT: **3.4 tok/s, 9.5 tok/s** with
  speculative decoding. This is the real protocol with compute time simulated from each GPU's
  memory bandwidth.
- 300 NAT'd nodes in 6 processes with 20–120 ms injected latency and churn: 98% of requests routed,
  97% after killing a relay mid-run. A fresh node's first chat took 2.9 s.

What's **not** done: the real-weights stage engine runs on CPU (a GPU/ggml stage engine is next), so
real 70B swarms aren't practical yet. Swarms support dense Llama/Qwen3 only (MoE models and Gemma run
direct). There are no public relays yet, no installer, and no contribution credits, and the protocol
hasn't had an independent security review. I'd love feedback on the placement rule and the threat model.

[LINK] · [VIDEO]

---

## LinkedIn

**A 70-billion-parameter model doesn't fit on your GPU. It might fit on ten of ours.**

I've been building Mycel: a mesh where people share spare GPU time, owned by no one. Its
centrepiece is *swarm inference*. Models too big for any one machine get split layer by layer
across members' GPUs. Your own device keeps your words: it turns text into numbers, sends the
numbers through the chain, and turns the result back into text.

Three things I care about:

🧩 **It organizes itself.** No coordinator decides who hosts which layers. Every member reads the
same map and fills the thinnest spot. Members join, the model assembles. Someone leaves, it heals,
even mid-sentence.

🔍 **It shows its work.** Every reply comes with its exact path: who computed which layers, how long
each hop took, what was re-routed, and what was independently cross-checked.

🔒 **It's private by default for everyone**, including the people serving. They share nothing about
their device or location unless they choose to, and they open no ports.

Status: a working prototype, tested end to end on one machine with simulated networks. A real model
split across four nodes gives token-for-token identical output to running it in one place. Next up
is a GPU engine for the stages, and the first public relays.

Demo video 👇 (the network in it is simulated) [VIDEO]

---

## Short (X / Bluesky / Mastodon)

```
Your GPU has 8 GB. Llama 70B needs 48.

Mycel splits big models layer-by-layer across many people's GPUs — your device keeps the words,
the swarm only sees numbers. Self-organizing, self-healing, and every reply shows the path it took.

Prototype, tested on one machine: [LINK]
```

```
Things I learned building split inference over a P2P mesh:
• latency, not bandwidth, is the wall (16 KB/token/hop)
• stage→stage "express lane": N+1 legs, not 2N → 2.3 → 3.4 tok/s
• speculative decoding on the client: → 9.5 tok/s
• decentralized placement needs a "lowest movable id moves first" rule or it opens gaps
```

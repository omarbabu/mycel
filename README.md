# Mycel

**Run a 70-billion-parameter model on a mesh of ordinary 8 GB GPUs — and keep your words on your own device.**

Mycel is one founderless network of people sharing spare GPU time. Small models run on a single
member's GPU. Models too big for anyone run as a **swarm**: each member holds a *slice* of the
model's layers, and your request flows through them in order. Your device does the tokenizing,
embedding and sampling, so **no node in the chain ever sees a word you type**. Every reply comes
with **the exact path it took**: who computed which layers, how long each hop took, what healed
and what was cross-checked.

No accounts. No company in the middle. No open ports on your machine.

```
            your device                                    the mesh (behind NAT, via relays, end-to-end encrypted)
 ┌───────────────────────────────┐
 │ "why do neurons fire          │     layers 1–11        layers 12–22               layers 70–80
 │  together?"                   │   ┌────────────┐     ┌────────────┐            ┌────────────┐
 │   │ tokenize · embed          │──►│ RTX 3070   │────►│ RX 6700 XT │──► · · · ─►│ M1 Pro     │──┐
 │   ▼                           │   │ (Berlin)   │     │ (Paris)    │            │ (Dublin)   │  │
 │ numbers only ─────────────────┘   └────────────┘     └────────────┘            └────────────┘  │
 │   ▲ LM head · sample · text  ◄─────────────────── activations, never text ─────────────────────┘
 │ "Neurons that fire together…" │
 └───────────────────────────────┘
```

> note: No public relays exist yet. Where they run, who pays and who operates them is a decision for launch. Real-internet tests have only been simulated locally.

## See it in one minute

```bash
pip install -e .                         # Python 3.11+
python3 -m mycel simulate --joined       # a 20-member global mesh on loopback → http://127.0.0.1:7800
```

Pick **Llama 3.3 70B · Swarm** in the model menu. Watch the layer bar fill as your prompt
crosses six members' GPUs, then tap **Path** on the reply. In the simulation, members sit
behind NAT with *no listening ports*, reach each other only through two local relays, see
real-world latency from the distance between their cities, and churn in and out. Watch a stage
die mid-reply and the swarm heal around it.

**Then prove it's real.** With any Qwen3 or Llama GGUF on disk (e.g. Qwen3 1.7B, served once in the app):

```text
$ mycel swarm-demo --stages 4
Mycel swarm demo · Qwen3 1.7B (28 layers) split across 4 nodes, ~7 layers each
assembled in 13.5 s · coverage ████████████████████████████
  node kirap4  layers  1–7    16.9 ms/token compute
  node odrdhl  layers  8–14   17.5 ms/token compute
  node 5kuvun  layers 15–21   16.9 ms/token compute
  node gmlgqm  layers 22–28   20.5 ms/token compute

you: In two sentences: why do neurons fire together?
swarm: Neurons fire together to transmit information efficiently, allowing communication between
cells in the nervous system. This synchronization ensures rapid and precise signaling in the brain and body.

path: you (tokenize, embed) → kirap4 [1–7] → odrdhl [8–14] → 5kuvun [15–21] → gmlgqm [22–28] → you (sample)
  31 tokens · 10.1 tok/s · relay forwarded 2609 frames (7.7 MB) · plaintext seen by relay: none
✓ token-for-token identical to running all 28 layers in one place.
```

Real weights, four independent nodes with their own keys and caches, every byte sealed end to
end through a relay — and the output matches a single-process run token for token. Add `--heal`
to unplug a node mid-sentence and watch it recover.

## Two ways to run a model

| | **Direct** — one member's GPU | **Swarm** — many members' GPUs |
|---|---|---|
| For | Models that fit on someone's card (Qwen3 8B–30B, gpt-oss 20B/120B, Gemma 3) | Models too big for any one member (Llama 3.3 70B, Qwen3 32B on 8 GB cards) |
| Who sees your words | The one node that answers (it signs a receipt for exactly what it wrote) | **Nobody.** Your device tokenizes, embeds and samples; stages see only activations |
| Speed | Full speed of that GPU | Latency-bound: ~3.4 tok/s, ~9.5 with speculative decoding (simulated 70B, 8 hops, 30 ms) |
| If the node vanishes | Fails over before the first token | Heals mid-reply: re-routes the lost layers and replays context |
| In the app | "On the mesh" | Its own **Swarm** section, badge, colour and live layer bar |

In the API, add `@swarm` to any swarm-capable model id: `"model": "llama-3.3-70b@swarm"`.

## How a swarm decides who does what

**Nobody assigns layers.** Each hosting member gossips which slice it holds. Every host reads the
same coverage map and places itself on the **thinnest stretch** of layers:

- **Few members:** each takes as many layers as its GPU fits. The model is "62% assembled — needs ~16 GB
  more" until enough members join. The app shows exactly which layers are missing.
- **More members:** new hosts fill gaps first, then add replicas where coverage is lowest. Replicas
  mean more throughput and painless failover.
- **Someone leaves:** the hole appears in everyone's map, and idle hosts in over-served regions move
  into it. A host only moves if its own layers stay covered without it. Among overlapping hosts,
  only the lowest-id *movable* one goes first. Two hosts of the same layers never leave at once,
  so decentralized re-placement never opens a gap and never deadlocks. A host keeps serving its old
  slice until the new one is loaded.

**You choose the model; Mycel picks the layers.** In **Models → Host a slice of a giant**, an 8 GB
card is told something like *"Your device can hold 11 of Llama 3.3 70B's 80 layers."* One tap later
it's part of a model no single member could run.

**Your node plans each request's route:** a shortest-path search over layer boundaries, where each
edge is "member X computes layers a..b", priced at measured round-trip time plus compute per
layer. It favours few, fast, nearby hops and routes around busy or unreliable members. Then:

- **Express lane.** During decoding, each stage hands its output straight to the next one and copies
  it back to you in parallel. That's N+1 network legs per token instead of 2N.
- **Speculative decoding.** A small same-family model on your device drafts tokens, and the swarm
  verifies several per round trip. Output is identical at temperature 0 (tested), and the
  distribution is identical when sampling.
- **Self-healing.** If a stage drops, its layers are re-planned onto replicas and its context is
  replayed into them. No restart.
- **Cross-checks.** On every reply, one slice is recomputed by a randomly chosen independent
  replica. A mismatch brings in a tie-breaker. A node that returns wrong math is replaced before
  your reply starts and down-ranked on your device.

## Every reply shows its path

Tap the chip under any reply to see **How this reply was made**:

- **The route:** a card per hop, with layers, compute ms, network ms, and the transport (relay / stage → stage).
- **Who computed which of the 80 layers:** a colour-coded layer bar.
- **The map:** arcs between the members who chose to share a location. The others are counted, never drawn.
- **What happened:** failovers ("node rikqev dropped out at 5 s; layers 1–14 re-routed, 49 tokens
  replayed in 173 ms"), cross-checks, and speculation stats.
- **Who saw what:** in plain language, for that exact reply.

Direct and on-device replies get the same view, with a single hop.

## Private by default — including for people who serve

**Settings → What others see** lists exactly what your node broadcasts, with a live view of your
signed advert.

| Always shared (needed to route work and prove membership) | Off unless you turn it on |
|---|---|
| Node ID (a random key, not your name) · membership proof · protocol version · encryption key · what you serve and how busy you are · your relay | **Device model** and exact memory (otherwise just a coarse tier) · **Location** (a coarse cell at best, never coordinates, never looked up from your IP) · **Activity** count |

Behind a relay, **your IP address never appears in your advert.** Coordinates never leave your
device. The path view respects every member's choices: hidden hardware shows as "private", and
hidden locations aren't drawn.

## No open ports: relays and end-to-end encryption

Home machines open **no inbound ports**. A node keeps *outbound* WebSockets to two or three relays
and works behind any NAT. Every message is a signed envelope **sealed with HPKE**
(RFC 9180: X25519, HKDF-SHA256, ChaCha20-Poly1305, from `cryptography`; no custom crypto) to the
recipient's key, which is bound to its node id by its signed advert. Every response chunk is
authenticated under a fresh per-request key. Relays forward ciphertext by node id. They require a
signed challenge and a proof-of-work identity ticket, rate-limit each node, and bound every queue.
If all of a node's relays die, it adopts one it learned from peers. Anyone can run one:
`mycel relay`.

## Measured, not promised

| What | Result |
|---|---|
| Split-inference exactness | Qwen3 1.7B split 4 ways gives **token-identical** output to llama.cpp, for both Q8_0 and Q4_K_M (Q4_K/Q6_K decoders validated) |
| Tokenizer | Byte-level BPE straight from GGUF metadata, **identical ids** to `llama-tokenize` (emoji, CJK, code, chat special tokens) |
| Real swarm, this laptop (M3 Pro) | 4 nodes via relay: **19.1 tok/s** on the GPU engine (PyTorch/Metal, fp16) · 10.1 tok/s on the CPU engine · relay saw no plaintext |
| Simulated Llama 3.3 70B, 8 × 8 GB GPUs, 30 ms RTT | **3.4 tok/s** (express lane) · **9.5 tok/s** with speculative decoding (plan target: ≥ 3 / ≥ 5) |
| Internet-mesh harness: 300 NAT'd nodes, 6 processes, 2 relays, 20–120 ms latency, churn | 90% average live view · **98.5%** of routes OK, **97.4%** after killing a relay mid-run · a fresh node's first chat in **2.9 s** · zero plaintext at relays |
| GPU detection | From `llama-server --list-devices` (Metal, CUDA multi-GPU, ROCm, Vulkan). No PyTorch: this Mac reads 28.1 GB Metal, 150 GB/s, and is recommended Qwen3 30B-A3B |
| Tests | **56** end-to-end and unit tests, including failover mid-reply, a lying stage, speculation exactness, range-download of a single layer slice, and relay admission |

## Quick start: your real node

```bash
pip install -e .            # Python 3.11+
brew install llama.cpp      # the engine Mycel supervises (or your OS package)
mycel                       # = mycel start: detects your GPU, opens http://127.0.0.1:7800/
```

Pick a model in **Models**: Mycel downloads it, runs it on 127.0.0.1, and shares it within your
limits. Or host a slice of a giant. Your node stays loopback-only unless you configure relays
(`mesh.relays`).

```bash
python3 -m pytest -q tests/                          # 56 tests
python3 -m mycel simulate --joined                   # 20-member mesh, relays, NAT, latency, swarms (--direct for the old style)
python3 -m mycel swarm-demo [--heal] [--rtt-ms 30]   # split a real model across local nodes; prove exactness
python3 -m mycel relay --port 7600                   # run a relay (loopback unless --allow-public)
python3 -m mycel probe                               # what Mycel sees on this machine
python3 -I tools/relay_harness.py --nodes 300        # multi-process internet-mesh harness
python3 -I tools/shoot.py out swarm                  # UI screenshots (against a simulation)
```

## Three ways to take part

Using the mesh is always on. On top of that, your GPU can do any mix of:

| | What it means | Who sees it |
|---|---|---|
| 🔒 **Private model** | A model runs on your GPU just for you | Nobody. It isn't even named in your advert |
| ⚡ **Shared model** | Same model; members may use it within your limit | Members who route to you |
| 🧩 **Swarm slice** | You hold some layers of a giant model (Mycel picks which) | Swarm requesters, as activations only |

All three are switches in **the cockpit**, at the top of Network. The node orb in the header and
sidebar always opens it, so wherever you see "Sharing", "Private" or "Swarm host", one click
takes you to the switch that changes it. Inside the cockpit:

- **A live reactor** that spins up as the mesh uses your GPU.
- **Levels** (Seedling → Biome) for the tokens you've given, plus achievements that unlock with a burst.
- **One-tap "Swap for a slice"** when your GPU's memory is busy with a model.

## The app

| | |
|---|---|
| **Chat** | Streaming Markdown, visible thinking, tool cards with approval, ⌘K, history. The model picker groups *On this device* · *On the mesh* · **Swarm** (assembled, or "62% assembled" with a way to help). Swarm replies get a live layer bar and an iridescent provenance chip. |
| **Path** | The route viewer described above, for every reply. |
| **Models** | "How your GPU takes part" (the same three modules), then the catalog with fit and speed. Running a model asks **Run privately** or **Run & share**; the active one is marked *Active · private/shared*. |
| **Network** | **The cockpit** (your node: reactor, three modules, level, achievements), live tiles, a **Swarms** card with per-layer coverage (gaps hatched, your slice underlined), a map, and members with their slices. |
| **Plugins** | Built-ins plus MCP servers. They run on *your* device, with approval. |
| **Connect an agent** | OpenAI-compatible `/v1` on 127.0.0.1 with streaming, tools, and `@swarm` models. |
| **Settings** | Sharing limits (hard caps; swarm work is charged by the compute actually spent), chat privacy, **What others see**, membership, appearance. |

## Security model (enforced in code)

- **Exposure.** The app and `/v1` are loopback-only with no override (Host/Origin checks, JSON-only
  writes, strict CSP). Nodes need no listener at all. Relays and mesh listeners refuse wildcard
  binds. Relay URLs from adverts must be `wss://` to public addresses (DNS answers are filtered),
  and a loopback node never dials another machine.
- **Membership.** A founderless global mesh with proof-of-work identities, or private meshes with
  certificate chains and revocation. Non-members learn nothing.
- **Integrity.** Signed receipts for direct replies. For swarms: per-hop authenticated streams,
  session tokens, express-lane results accepted only from the right stage, and random
  cross-checks.
- **Limits.** Hard GPU-share budgets, deadlines, daily caps, per-member rates, session caps per
  stage.

## Honest limits

- **Stage engines hold weights in fp16 (GPU, via PyTorch) or fp32 (CPU, NumPy)** — 3–6× the memory
  of the 4-bit files, so an 8 GB card holds ~3 layers of a 70B model instead of ~11. The 70B speed
  numbers above are the real protocol with *simulated* compute. A ggml/llama.cpp stage engine that
  keeps 4-bit weights on the GPU is the next big step. Hosts fetch only their slice (HTTP range
  requests) and requesters only the embedding/head, on first use.
- **Swarms support dense Llama and Qwen3 today.** Mixture-of-experts models (gpt-oss, Qwen3 30B-A3B)
  and Gemma run direct-only.
- **Activations aren't encrypted *from the stage computing on them*.** "No node sees your words" is
  true. A determined adversary could still try to invert activations. For sensitive work, use your
  own GPU.
- **No public relays exist yet.** Where they run, who pays and who operates them is a decision for
  launch. Real-internet tests have only been simulated locally.
- **Not done:** a DHT for very large meshes, contribution credits, memory-hard identity puzzles,
  installers and auto-update, Windows support, and an **independent security review** of the
  envelope, transport, membership and receipt protocols.

## Layout

```
mycel/node.py          membership, gossip, sealed RPC dispatch, routing, direct serving, privacy
mycel/rpc.py           HPKE request sealing, authenticated response streams
mycel/transport.py     direct HTTP + outbound-only relay transport, WAN emulation, address safety
mycel/relay.py         the relay: admission, rate limits, rendezvous
mycel/swarm/           split inference
  gguf.py              GGUF reader/writer, dequantization, layer-slice range downloads
  engine.py            transformer stages (Llama/Qwen3), bookends, KV caches, simulated engine
  tokenizer.py         byte-level BPE + chat templates + output parsing, from GGUF metadata
  planner.py           coverage, decentralized placement, route planning
  host.py              hosting a slice: sessions, budget, express lane
  client.py            pipelines: failover, cross-checks, speculation, path traces
  demo.py              `mycel swarm-demo`
mycel/hardware.py      GPU detection via llama.cpp, bandwidth table
mycel/static/          the app (vanilla ES modules, no CDN); path.js = the route viewer
tools/                 relay_harness.py, scale_test.py, shoot.py, record.py
launch/                launch posts and video
docs/STATE_AND_PLAN.md where things stand and what's next
```

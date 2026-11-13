# Mycel: current state and plan forward

*Updated 2026-11-04. Repo: `/Volumes/workplace/artifacts/mycel` (~7.8k lines Python, ~3.1k lines JS/CSS, 52 tests).*

## 1. Goal

A new user reads about Mycel, installs it, taps Join, and chats with models served by other
people's GPUs, **including models bigger than any one member's GPU (swarms)**. It must be
private by default for requesters and for the people who serve.

**Status:** everything is built and tested on one machine, with relays, NAT and WAN latency
simulated locally. It is **not yet usable across the internet**: no public relays exist, and the
real-weights swarm engine runs on CPU.

## 2. What was done in this round (P1–P3 of the previous plan)

| Plan item | State |
|---|---|
| **P1 GPU detection without PyTorch** | Done. `llama-server --list-devices` is parsed for Metal, CUDA (multi-GPU), ROCm and Vulkan (deduplicated), with a bandwidth table for ~90 chips. Bandwidth is back-solved from the measured tok/s once a model serves, and persisted. The TFLOPS requirement is gone. This Mac: 28.1 GB Metal, 150 GB/s, recommended Qwen3 30B-A3B. |
| **P2 transport** | Done (locally). HPKE (RFC 9180) end-to-end sealing of every request, with per-request authenticated response streams (`rpc.py`). `Transport` layer with direct HTTP plus **outbound-only relays** (`transport.py`, `relay.py`): signed-challenge plus PoW admission, rate limits, bounded queues, rendezvous, visiting other relays, and adopting a learned relay if all of a node's relays die. Nodes can run with **no listener at all**. Relay-URL SSRF guard: wss + public addresses only, and DNS answers filtered. |
| **P2 testing** | `tools/relay_harness.py`: 300 NAT'd nodes in 6 processes plus 2 relay processes, 20–120 ms latency, churn, one relay killed mid-run. Routes OK 98.5% before the kill and 97.4% after; first chat for a fresh node 2.9 s; zero plaintext at relays. |
| **P3 split inference** | Done (reference engine). GGUF reader/writer with all catalog quant types (Q8_0, Q4_K, Q6_K verified **token-identical to llama.cpp**). NumPy Llama/Qwen3 stages. BPE tokenizer **identical to llama-tokenize**. Requester-side bookends, so no node sees text. Decentralized placement with a gap-free, deadlock-free move rule. Shortest-path route planning. **Express lane** (stage → stage, N+1 legs). Failover with KV replay. Random cross-checks with a tie-breaker. Speculative decoding (exact). Layer-slice HTTP range download. `mycel swarm-demo` runs real Qwen3 1.7B across 4 nodes at 10 tok/s, identical to a single process. |
| **P3 acceptance (simulated compute)** | Llama 3.3 70B over 8 × 8 GB at 30 ms RTT: **3.4 tok/s** plain, **9.5 tok/s** speculative, survives killing a stage mid-reply (tested). |
| **UX (user request)** | Swarm is visually distinct everywhere: its own picker section, badge, colour, route chip, live layer bar, explainer, hero banner, Network "Swarms" coverage card and Models "Host a slice". **Path viewer** for every reply: route cards, layer map, map arcs, failovers, cross-checks, speculation, who-saw-what. **What others see** settings: device, location and activity off by default; node id and routing facts always on; a live view of the signed advert; no IP address behind relays. |
| **Simulation** | `mycel simulate`: 20 members, 2 relays, NAT, latency by city distance, a European 70B swarm cluster, a still-assembling Qwen3 32B, churn, an intruder. `--direct` gives the old mode. |

**UX round 2 (2026-11-04):** three explicit roles (private model / shared model / swarm slice). A private
model is hidden from the mesh: no models or role advertised. Swarm hosting has its own switch,
independent of "paused" (which now means *private*). The Network **cockpit** gathers the reactor
animation, three control modules, levels and achievements. Header and sidebar indicators open it.
Models shows "Active · private/shared" with a Run privately / Run & share choice, and "Swap for a
slice" frees the active model's memory (`POST /api/swarm/host {replace_active: true}`). Effects live
in `static/js/fx.js` and respect reduced motion.

**Round 3 (v0.5.0):**
- **GPU stage engine.** `TorchStage` uses PyTorch on Metal/CUDA in fp16, with fused QKV and
  gate/up, fused RMSNorm and SDPA, and RoPE tables on the device. Real Qwen3 1.7B across 4 nodes:
  19.1 tok/s (CPU engine: 10.1), exact within the engine, token-identical output. `engine: auto`
  picks torch when a GPU is available.
- **Correct memory sizing per engine** (fp16 / fp32 / simulated Q4). A layer-count control lets
  you host 1..N layers.
- **Real slice and bookend downloads** via HTTP range from Hugging Face (certifi TLS) into
  `<data>/slices`.
- **UI.** A single power switch for Private/Shared that works even against an older node, a
  version-mismatch banner, a WebGL aurora and plasma-orb reactor, a dotted 3D globe with swarm
  arcs, and a token "beam" on swarm replies.
- **Fix.** A saved slice that no longer fits now shows an error instead of crashing node startup.

## 3. Known gaps (ordered by impact)

1. **Quantized GPU stage engine.** The GPU engine exists (fp16 torch), but it needs about 3× the
   memory of Q4, and decode at batch 1 is launch-bound (~1 ms per layer of overhead on Metal). A real 70B swarm needs a stage engine that keeps
   quantized weights on the GPU. Options: a ggml-based stage server (llama.cpp's graph restricted to
   a layer range; the closest fit, since we already ship llama.cpp), or torch with fp16/bnb weights
   as an interim. The planner, protocol and app don't change.
2. **Public relays (ops decision for the user).** Where the first 2–3 relays run, who operates them,
   cost, a status page and abuse handling. The seed list ships empty today, so `mycel start` stays
   loopback-only.
3. **Swarm architectures.** MoE (gpt-oss, Qwen3 30B-A3B) and Gemma 3 are direct-only.
4. **Bookends for big models on weak clients.** A 70B requester holds ~1.3 GB (embedding plus head).
   NumPy fp32 makes that 4 GB+. It needs a quantized head matvec, or an optional "last stage applies
   the head" mode, which trades away output-token privacy.
5. **Activation privacy.** Stages see activations, which can be inverted by a determined adversary.
   The app says so.
6. **Trust and fairness.** No contribution credits. SHA-256 PoW isn't Sybil-proof (memory-hard
   puzzles are planned). Cross-checks cover one slice per reply.
7. **Scale beyond ~1k.** No DHT for "who hosts model X" yet. Gossip views are bounded at 2,000.
8. **Packaging.** No installers or auto-update. Windows process supervision is untested.
9. **Independent security review** of the envelope, HPKE usage, relay admission, express-lane
   tokens and receipts.
10. **Legal.** Amazon outside-activity and open-source approval (this was built on a company laptop),
    trademark and domain, and model licences for a public serving network.

## 4. Plan forward

1. **P4a: GPU stage engine** (biggest technical unlock). Prototype a ggml stage server that loads a
   GGUF layer range plus KV cache on Metal/CUDA and exposes `forward(pos, hidden)`. Acceptance: the
   same `test_swarm` suite passes against it, and Qwen3 1.7B split 4 ways runs ≥ 40 tok/s on this
   Mac and matches the NumPy engine to within 1e-3.
2. **P4b: first real-internet trial**, once the user approves relay hosting. Two relays on approved
   infrastructure. Two machines on different networks join with one tap and chat within 60 s.
   Neither opens an inbound port. This laptop never runs a public relay.
3. **P5: trust.** Contribution credits from signed receipts and stage work. Memory-hard identity
   puzzles. Cross-check rate driven by reputation. External security review.
4. **P6: distribution.** Signed macOS app (bundled Python + llama.cpp), then Linux and Windows.
5. **P7: discovery at scale.** DHT keyed by model and slice, plus digest anti-entropy.

## 5. Notes for the next session

- **The user's real node** may be running (`python3 -m mycel start`, data `~/.mycel/default`,
  ports 7700/7800). Never point tools at it. Use `mycel simulate --joined --control-port 8000
  --mesh-port 7900` and `MYCEL_URL=http://127.0.0.1:8000`. The running real node uses the
  *previous* code until restarted.
- **Everything is loopback.** Relays bind 127.0.0.1, nodes in simulations have no listener, and a
  loopback node refuses to dial other machines. Keep it that way.
- `mycel swarm-demo` reads the Qwen3 1.7B GGUF that llama-server downloaded into `~/.mycel/default/models`.
- **Test gotchas:** give every test node its own data dir (saved settings override config), and
  wait for stable placement before asserting on routes.

---

## 6. Handoff: full state at v0.5.0 (2026-11-04), written so a fresh session needs nothing else

### 6.1 What Mycel is

A founderless mesh where people share spare GPU time. Three ways a member's GPU takes part (using the
mesh is always on):

1. **Private model.** A model runs on your GPU only for you. It isn't advertised (no model name, role
   `client`).
2. **Shared model.** The same model, which members may use within your GPU-share limit. In the
   backend, `limits.paused = false` means shared.
3. **Swarm slice.** You host a contiguous range of layers of a model too big for one GPU. Mycel picks
   which layers; you pick the model and how many.

Requests run **direct** (one node runs the whole model) or as a **swarm** (`model@swarm`: a chain of
stages; the requester tokenizes, embeds and samples, so stages see only activations).

Codebase: ~8.0k lines Python, ~3.9k lines JS/CSS, 56 tests (`python3 -m pytest -q tests/`, ~70 s).
It is not a git repo.

### 6.2 Architecture map

| File | Role |
|---|---|
| `mycel/node.py` | Node: membership, settings persistence, adverts (privacy-filtered), sealed RPC client/server dispatch (`gossip`, `infer`, `stage.forward`, `stage.close`, `stage.result`), direct routing, `route()` (direct, falling back to swarm), views and stats. `Node.sharing` = serving and not paused. |
| `mycel/rpc.py` | HPKE (RFC 9180, X25519/HKDF-SHA256/ChaCha20-Poly1305 via `cryptography>=48`) request sealing; per-request response key; authenticated ordered response chunks; terminal `_end` item guards against truncation. |
| `mycel/transport.py` | `HTTPTransport` (mesh listener, loopback/overlay) and `RelayTransport` (outbound WebSockets: home relays, visiting peers' relays, rendezvous, healing that adopts a learned relay if all die). `Netem` WAN emulation. Relay URL guard: wss + public IPs only, DNS answers filtered; loopback nodes connect only to loopback. |
| `mycel/relay.py` | `mycel relay`: signed-challenge + PoW-ticket admission, per-node byte/frame token buckets, bounded queues, nack, `peers` rendezvous, `/health`, test-only audit log. Binary frame: kind, id, 8-byte stream id, payload. |
| `mycel/swarm/gguf.py` | GGUF v2/v3 reader/writer. Dequant F32/F16/BF16/Q8_0/Q4_0/Q4_1/Q5_0/Q5_1/Q4_K/Q5_K/Q6_K (Q8_0, Q4_K and Q6_K verified token-identical against llama.cpp). `fetch_slice()` uses HTTP Range to fetch the header plus only the tensors for layers [s,e), or bookends with `s=e=0, bookends=True`. |
| `mycel/swarm/engine.py` | `GGUFStage` (NumPy fp32, CPU), `TorchStage` (PyTorch fp16 on Metal/CUDA: fused QKV and gate/up, `F.rms_norm`, SDPA with GQA, device RoPE tables), bookends for both, `LocalModelAny` (drafts/reference), `MockStage`/`MockBookends` (simulations), `torch_device()`, `make_stage()`. |
| `mycel/swarm/tokenizer.py` | Byte-level BPE from GGUF metadata (identical ids to `llama-tokenize`), Qwen3/Llama-3 chat templates (Qwen tools format; non-thinking by default), streaming `OutputParser` (think / tool_call / text). |
| `mycel/swarm/planner.py` | `spans_for`, `coverage`, Dijkstra `plan()` over layer boundaries (cost = RTT + hop overhead + ms/layer, divided by reliability), `choose_span` (thinnest window, aligned to neighbours), `better_span` (move only if your layers stay covered and no lower-id *movable* overlapping host exists: no gaps, no deadlock), `overview()` for the UI. |
| `mycel/swarm/host.py` | `SwarmHost`: model + span + engine. `max_layers()` sizes by engine (`CatalogModel.layer_gb_for`); `layer_capacity()` = min(chosen `swarm.layers`, fit). Downloads the slice if no local GGUF. Settle/rebalance loop. Moves are double-buffered. Session tokens. Express-lane forwarding (`_express`). Compute is charged to the governor per forward. Startup never fails on a slice that doesn't fit (status error). |
| `mycel/swarm/client.py` | `Pipeline`: orchestrated `forward` (prefill, replay), `forward_express` (decode, stage→stage plus copies back), `_failover` (re-plan the lost range, replay inputs; tolerant of 404/409 "moved"), `crosscheck` (random replica, tie-breaker, liar replacement plus downstream rebuild), `trace()`. `SwarmClient` runs mock or real generation, speculative decoding (`_propose`/`_verify`, exact at temperature 0), bookend auto-download on first use. |
| `mycel/swarm/demo.py` | `mycel swarm-demo [--stages N] [--engine auto/torch/gguf] [--heal] [--rtt-ms X]`: real weights over a loopback relay, compared with a single-process reference. |
| `mycel/hardware.py` | GPU detection from `llama-server --list-devices` (Metal/CUDA/ROCm/Vulkan, dedup, multi-GPU sum) plus a bandwidth table; bandwidth back-solved from measured tok/s; PyTorch probe as an optional fallback. |
| `mycel/models.py` | 13-model catalog. Swarm-capable (dense Llama/Qwen3): llama-3.3-70b, qwen3-32b, qwen3-14b, qwen3-8b, qwen3-4b, llama-3.2-3b, qwen3-1.7b. `layer_gb` (Q4 file share), `layer_gb_for(engine)`, `bookend_gb`, `est_layer_ms`. `register()` for tests. |
| `mycel/api.py` | Loopback API. New: `/api/visibility` GET/POST, `/api/swarm` GET (rows with `capacity_layers`, `capacity_if_free`, `blocked_by_active`, `layer_gb`, `download_layer_gb`, `engine`), `/api/swarm/host` POST `{model or null, layers?, replace_active?}`, `/api/library/serve {id, share?}`, `/api/status` includes `sharing`, `swarm`, `visibility`, `network`. `/v1` exposes `id@swarm`. |
| `mycel/cli.py` | `start`, `run`, `probe`, `relay`, `swarm-demo`, `simulate` (20 nodes; 2 relays; NAT; city-distance latency; European 70B swarm cluster plus an assembling Qwen3 32B; churn; intruder; `--direct`, `--fresh`, `--joined`, `--private`). |
| `mycel/static/js/` | `cockpit.js` (Network cockpit plus the three modules, also used on Models), `gl.js` (WebGL aurora, plasma orb, dotted 3D globe), `fx.js` (count-up, bursts, mesh field, 2D reactor fallback, levels, achievements), `path.js` (route viewer), `nodepanel.js` (`nodeState`, orb, header button, route popover), `chat.js`, `models.js`, `network.js`, `settings.js` ("What others see"), `onboarding.js`. `main.js` has `APP_VERSION = "0.5.0"` (shows a banner if the node's version differs). |
| `tools/` | `relay_harness.py` (multi-process internet-mesh harness), `shoot.py` (headless-Chrome screenshots; scenarios `swarm`, `cockpit`, `glcheck`, `onboarding`, view lists), `record.py` (launch video against `simulate --fresh` on port 8000), `scale_test.py`. |
| `launch/` | `launch_posts.md`, `video/mycel-launch.mp4` (81 s, **shows v0.4 UI: re-record before posting**), `video/cards.html`. |

### 6.3 Verified results (all on this M3 Pro, loopback only)

| What | Result |
|---|---|
| Engine exactness | Qwen3 1.7B (Q8_0 and a local Q4_K_M requant) split 4 ways is token-identical to llama-server. |
| Real swarm, GPU engine | 4 nodes via relay: 19.1 tok/s, first token 1.1 s, identical to a single-process run. |
| Real swarm, CPU engine | 4 nodes via relay: 10.1 tok/s. |
| GPU engine alone | 23.7 tok/s decode (4 in-process stages, no network). |
| Simulated 70B (mock compute from bandwidth) | 8 × 8 GB at 30 ms RTT: 3.4 tok/s express lane (2.3 without), 9.5 tok/s with speculation. |
| Relay harness | 300 nodes / 6 processes / 2 relays / 20–120 ms / churn: 98.5% routes OK, 97.4% after killing a relay, first chat 2.9 s, zero plaintext. |
| Hardware detection | 28.1 GB Metal, 150 GB/s, recommends Qwen3 30B-A3B. |
| The user's node | Can host 10 of 80 Llama 70B layers alongside Qwen3 1.7B (12 if swapped): 1.8 GB/layer fp16, 0.57 GB/layer download. |

### 6.4 Why slices are held at 16-bit today (and how to fix it)

Model files are 4-bit (Q4_K_M ≈ 4.8 bits/weight). Our stage engines **dequantize at load**:
TorchStage to fp16 (≈ 3.3× the file size per layer), GGUFStage to fp32 (≈ 6.6×). PyTorch (and
NumPy) have no fast kernels for llama.cpp's Q4_K block formats, especially on Metal. Matmuls on
dequantized weights were the quickest way to get a correct, verifiable engine.

Cost: an 8 GB card holds ~3 layers of Llama 70B instead of ~11, and decode reads 3.3× more bytes per
token. Fix options, best first:
- **(a) ggml stage server.** llama.cpp's own Metal/CUDA Q4 kernels run a layer range. Either a small
  C++ binary linked to ggml that loads a slice GGUF and exposes `forward(pos, hidden)` over a local
  socket, or use llama.cpp's graph with layer offsets. Keeps 4-bit, fastest, same files.
- (b) Port Q4_K/Q6_K matvec to Metal/CUDA kernels callable from PyTorch (custom ops). Harder to maintain.
- (c) int8 weight-only quantization in torch (`int8 × scale`, dequantized per matmul). 2× better than
  fp16, easy, slower than ggml.

### 6.5 Production readiness: NOT production ready

It's a well-tested prototype on one machine. Blocking a real out-of-the-box launch:

1. **No internet connectivity in practice.** There are no public relays, and the seed list is empty.
   A new user is alone. Needs: 2–3 relays on approved infrastructure (cost, operator, status page,
   abuse contact), a signed seed list in the app, and a real two-network trial (P4b). This laptop
   must never host a public relay.
2. **Swarm compute isn't production grade.** fp16 memory (6.4), launch-bound decode (~1 ms/layer
   of overhead on Metal), and real 70B swarm speed unmeasured. Needs a 4-bit GPU stage engine and a
   measured multi-machine 70B run.
3. **Bookends for big models.** A 70B requester downloads ~1.3 GB (embed + head) and holds it at fp16
   (~4 GB). Weak laptops need a quantized head, or an opt-in "last stage applies the head" mode that
   trades away output-token privacy.
4. **Packaging.** No installer. Today it needs Python 3.11+, `pip install -e .`, Homebrew llama.cpp
   and optionally torch. Needs a signed and notarized macOS app bundling Python, llama.cpp (Metal) and
   the stage engine; auto-update with signed releases; then Windows (CUDA/Vulkan; process supervision
   without POSIX signals) and Linux.
5. **Security.** No independent review of the envelope, HPKE use, relay admission, express-lane
   tokens, receipts or the membership PoW. SHA-256 PoW is cheap on GPUs (memory-hard puzzle planned).
   Cross-checks cover one slice per reply. MCP servers aren't sandboxed.
6. **Abuse and fairness.** No contribution credits or priority. No moderation story for a public
   serving network (what's legal to serve and generate, and reporting).
7. **Coverage gaps.** Swarm supports only dense Llama/Qwen3 (no MoE/Gemma). Real MCP catalog servers
   are untested. CUDA/ROCm/Windows are untested. There's no DHT beyond ~2k-node bounded views.
8. **Legal and business.** Amazon outside-activity and open-source approval (built on a company
   laptop), "Mycel" trademark and domain, model licences (Llama Community, Gemma Terms) for a
   public network, privacy policy.
9. **Launch assets.** Re-record the video for v0.5. Re-check the posts against the actual state.

### 6.6 Next steps, in order, with acceptance criteria

1. **4-bit GPU stage engine (ggml).** `make_stage("ggml", ...)` passes `tests/test_swarm.py`
   unchanged. Qwen3 1.7B split 4 ways ≥ 40 tok/s on this Mac. `layer_gb_for("ggml") ≈ layer_gb`, so
   an 8 GB card holds ~11 Llama-70B layers. Matches the torch engine within fp16 tolerance.
2. **Decode latency.** Pin sessions to a warm path, cut per-hop serialization (binary activations
   instead of base64 JSON inside the sealed envelope), and pipeline speculative rounds. Target: the
   real-weights express lane within 20% of the simulated numbers.
3. **First internet trial (needs the user's go-ahead on relay hosting).** Two relays; two machines on
   different networks join with one tap and chat within 60 s; no inbound ports. Then a measured
   multi-machine swarm on real weights (start with Qwen3 32B across 3–4 Macs).
4. **Out-of-the-box install.** A signed macOS `.app` that bundles everything. First run: open →
   Join → recommended model (or slice) → chat in under 2 minutes, no terminal.
5. **Retention and fairness.** Contribution credits from receipts and stage work → priority and
   levels (the UI's levels and achievements already exist client-side), uptime streaks, and
   notifications when your GPU helped someone.
6. **Trust.** External security review, memory-hard identity puzzles, reputation-driven cross-check
   rate, MCP sandboxing.
7. **Breadth.** MoE swarm support (expert-parallel or layer-parallel for gpt-oss/Qwen3-30B-A3B),
   Gemma 3, DHT discovery, Windows/Linux builds.

### 6.7 Operating rules and gotchas for the next session

- **The user's real node** runs from the repo dir: `cd /Volumes/workplace/artifacts/mycel && nohup
  python3 -m mycel start --no-open > /tmp/mycel_real.log 2>&1 &`. Running `python3 -m mycel` from the
  *parent* dir fails, because the repo folder shadows the package. Data is in `~/.mycel/default`
  (Qwen3 1.7B is downloaded and active, currently Private), on ports 7800 (app) and 7700 (mesh), both
  127.0.0.1. Restart it after backend changes, since the static files update live but the Python
  doesn't. Stop with SIGTERM.
- **Never point tools at the real node.** Use `python3 -m mycel simulate --joined --control-port
  8000 --mesh-port 7900` (or `--fresh` for onboarding) and `MYCEL_URL=http://127.0.0.1:8000`. The
  simulation's "you" persists in `~/.mycel/sim-you` (saved settings carry over between runs).
- **Network rules.** Loopback only: no non-loopback binds, no pinging other machines, simulations only.
  Pressing "Host a slice" on the real node starts a real Hugging Face download (user-initiated only).
  Python's urllib lacks CAs here, so weight downloads use certifi. Never disable TLS verification.
- **Tests.** Tiny real-format GGUF models come from `mycel/swarm/tiny.py`. Real-weight test engines
  use explicit layer counts (`LAYERS_FOR_BUDGET` in `tests/test_swarm.py`). Give every test node its
  own data dir. Wait for stable placement before asserting.
- **Screenshots.** `tools/shoot.py` runs headless Chrome with swiftshader (WebGL works).
  `glcheck` reports whether the orb, aurora and globe canvases exist.
- **User preferences.** Very high bar for UX ("cinematic", game-like, intuitive, retention matters),
  but technical capability (fast, correct split inference) matters most. Concise communication.
  Honest claims only; demo footage is labeled as simulated.

---

## 7. Update 2026-11-09: internet-ready code, the Amazon path, and the gap to the vision

*Supersedes the connectivity and launch parts of §§3–6 where they conflict. 62 tests pass
(`python3 -m pytest -q tests/`, ~70 s). Version string is still 0.5.0.*

### 7.1 The vision, stated as an acceptance test

> A new user installs Mycel, taps **Join**, and within a minute is chatting with models served by
> other members' GPUs over the internet. Members together run models bigger than any single
> member's GPU (swarms). Nobody opens a port, and no node sees a requester's text in a swarm.

Today this works **on one machine only** (simulations, plus real weights across local processes).
It has never run between two real machines.

### 7.2 What Mycel is today (features)

**App** (vanilla JS, served on `127.0.0.1:7800`; the control plane is always loopback):
- **Onboarding.** Join the global mesh with one tap: a ~1 s proof-of-work identity, no account.
  Then the recommended model for your GPU (or "chat with the mesh" if you can't serve), a
  sharing level, and an optional coarse map location.
- **Chat.**
  - Streaming conversations stored only on this device.
  - "Private first" routing: your own GPU answers when it serves the model.
  - Tool use through plugins, with approval for each permission.
  - A **path viewer** for every reply: route, layer map, failovers, cross-checks, speculation,
    and who saw what.
- **Models.**
  - 13-model catalog (gpt-oss 120B/20B, Llama 3.3 70B, Qwen3 32B / 30B-A3B / 14B / 8B / 4B / 1.7B,
    Gemma 3 27B/12B/4B, Llama 3.2 3B).
  - One-click download and run via a supervised `llama-server` (Metal/CUDA/ROCm/Vulkan detection).
  - Each model runs **private** or **shared**.
  - **Host a slice** of a swarm model: you choose the model and how many layers; Mycel chooses
    which layers. Only that slice is downloaded (HTTP range).
- **Network cockpit.** Live orb and state, a power switch, three modules (your model, swarm
  slice, your limit), levels and achievements, a WebGL globe with swarm arcs, and mesh stats.
- **Settings.**
  - Sharing limit (governor caps GPU time per peer and per job).
  - Chat privacy.
  - "What others see": device, location and activity are off by default.
  - Membership: global mesh, or private invite-only meshes with cert chains, revocation and blocking.
  - **Network (new):** relays, with add/remove and a seed toggle.
  - Appearance.
- **Plugins.**
  - Built-ins: calculator, clock, units, notes, mesh insights.
  - MCP reference servers: fetch, files, git, memory, time, sequential thinking.
  - Custom MCP servers. These aren't sandboxed, and the real catalog servers are untested.
- **Agent API.** OpenAI-compatible `/v1` on loopback; `model@swarm` selects split inference.

**Mesh and security:**
- HPKE end-to-end sealing of every request and response stream.
- Signed adverts, gossip membership (views capped at 2,000) and proof-of-work identities.
- Signed receipts, random cross-checks of swarm stages, and the per-peer governor.
- DNS-rebinding and Origin guards on the control plane.
- An SSRF guard on relay URLs: wss only, public addresses only, DNS answers filtered.

**CLI:**
- `mycel start`: your node.
- `mycel relay`: run a relay.
- `mycel simulate`: a 20-node local mesh with NAT, relays, WAN latency and churn.
- `mycel swarm-demo`: real weights split across local processes.
- `mycel probe`: hardware check.

### 7.3 Does split inference across layers work today?

**Yes, functionally, with real weights, on one machine.**

**Verified:**
- `mycel swarm-demo` splits Qwen3 1.7B across 4 node processes talking through a loopback relay.
  Output is **token-identical** to llama.cpp.
- Speed: 19.1 tok/s with the GPU engine (torch fp16, Metal), 10.1 tok/s with the CPU engine.
- Failover with KV replay, cross-checks, speculative decoding and the express lane all run on
  real weights.
- Placement is decentralized and gap-free; requester-side bookends mean stages never see text.

**Not yet true:**
1. **Never run across two machines**, never over a real WAN, and never with a model too big for one node.
2. **The 70B figures are simulated compute** (3.4 tok/s plain, 9.5 with speculation, over
   8 × 8 GB at 30 ms RTT).
3. **Stages hold weights at fp16**, about 3.3× the 4-bit file. A 32B model needs ~65 GB across
   the swarm instead of ~20 GB, so "collectively bigger" needs ~3× more members than it should.
   Decode is launch-bound at ~1 ms per layer of overhead on Metal.
4. **Swarms support only dense Llama/Qwen3.** MoE (gpt-oss, Qwen3-30B-A3B) and Gemma are direct-only.
5. **Weak clients struggle with big-model bookends.** The requester holds the embedding and
   head: ~1.3 GB download for 70B, ~4 GB in RAM at fp16.

### 7.4 What changed this round (code, all tested locally)

**Internet mode** (`MeshConfig.internet`, now the default for `mycel start`; `--local` gives the
old loopback mesh):
- The node opens **no port at all**: no mesh listener, only the loopback app.
- It contacts relays **only after you join a mesh**, and drops them when you leave.
- The relay URL guard applies: wss only, public addresses, DNS filtered.

**Relay book** (`mycel/relays.py`, `<data>/relays.json`):
- Sources in priority order: yours (added in the app), `--relay` flags, bundled `mycel/seeds.json`
  (**empty until relays are approved and deployed**), and relays *learned* from peers' adverts
  that the node actually reached.
- Learned relays persist for 30 days, so after one successful join a node no longer depends on
  the seeds.
- The node keeps 3 home connections; healing adopts any known relay if every home relay is down.

**Network API:**
- `GET /api/network`.
- `POST /api/network/relays {url}`, `POST /api/network/relays/remove {url}`, `POST /api/network/bundled {on}`.
- `/api/status.network` is now `node.network_view()`.

**Relay hardening for public operation** (`mycel/relay.py`):
- Caps on unauthenticated handshakes, globally and per address, applied *before* the WebSocket upgrade.
- Per-address handshake rate and connection caps.
- `/health` returns 503 while draining; the public `/status` shows counters only (no ids or IPs).
- SIGTERM drains: health goes 503, then connections close with "going away" (1001).
- `proto` is sent in the challenge.
- `--behind-load-balancer`: allows a 0.0.0.0 bind inside the container and trusts only the
  last X-Forwarded-For hop. `--id` sets a stable relay id.
- The relay logs a once-a-minute counter line.

**Deploy kit** (`deploy/relay/`): a Dockerfile (non-root, read-only root filesystem) and a Python
CDK stack. It synthesizes cleanly; **nothing is deployed**.
- ALB on 443 only with TLS 1.3, WAF (IP reputation plus a per-address connect rate) and
  deletion protection.
- One single-task Fargate service and one host name per relay URL; deploys run at 100% max, so
  a relay never splits.
- Isolated subnets: no public IP and no internet route (VPC endpoints only).
- Task security group allows traffic from the ALB only.
- Flow logs, ALB logs (30 days, private, TLS-only bucket) and alarms.
- `README.md` covers verification steps and cost: ~$105/month for 2 relays plus $0.09/GB out,
  which dominates at scale.

**UI:**
- A new "searching" state ("Finding the mesh" / "No relays yet") on the orb, header, cockpit
  and onboarding.
- Settings → Network.
- The "Connected" badge and the "On the mesh" achievement now require a reachable relay.
- Fixed literal "null" text in cockpit cards (`Element.append(null)`).

**Tests:** `tests/test_internet.py` (6 tests) covers URL policy, book priorities and
persistence, join-before-relay consent, runtime add/remove, handshake caps, draining, the status
endpoint's privacy, and XFF handling.

### 7.5 How a new user would onboard (target flow), and where it stops today

1. **Install.** Target: a signed app with bundled Python and llama.cpp. *Today: Python 3.11+,
   `pip install -e .`, Homebrew llama.cpp, optional torch, then `mycel start`.*
2. **Join.** Target: identity minted, connected to seed relays, gossip finds members within
   seconds. *Today: identity works, then the app honestly shows **"No relays yet"**, because
   `seeds.json` is empty and no relay exists.*
3. **Pick a role.** Target: the recommended model downloads and runs; choose private or shared,
   or host a slice of a giant model. *Works today, locally.*
4. **Chat.** Target: direct models route to the best member; `model@swarm` builds a chain of
   members. *Works today only in simulations and local multi-process demos.*

### 7.6 The gap to the vision, in critical-path order

| # | Gap | Why it blocks | Work |
|---|---|---|---|
| 1 | **No live relays** | Nobody can meet anybody | Approvals (§7.7), then deploy `deploy/relay` in 2 regions and put the URLs in `seeds.json` |
| 2 | **No real multi-machine run** | Every claim is still single-machine | 2-machine join-and-chat trial, then a real swarm across 3–4 Macs (Qwen3 32B) |
| 3 | **fp16 stage engine** | Needs 3× the members per model, and slow decode | 4-bit GPU stage engine. MLX on Apple Silicon (native 4-bit quantized matmul, no ggml port needed) is the fastest route to try first. ggml (llama.cpp layer range) for CUDA/Vulkan. Target: `layer_gb_for(engine) ≈ layer_gb`, ≥ 40 tok/s for the 4-way 1.7B demo |
| 4 | **Licence-safe flagship** | Llama is prohibited for Amazon production use (§7.7), and it is the only 70B swarm model | Make Qwen3 32B the first "bigger than you" swarm. For giants, add **MoE swarm support** (gpt-oss-120b, Qwen3-235B-A22B, both Apache-2.0): layer-parallel first, experts kept with their layer |
| 5 | **WAN decode latency** | Real RTTs multiply per token | Binary activations (not base64 JSON) inside the envelope, warm pinned paths, pipelined speculation. Target: within 20% of the simulated numbers |
| 6 | **Weak-client bookends** | Laptops can't hold a 70B head at fp16 | Quantized head matvec, or an opt-in "last stage samples" mode (trades away output privacy) |
| 7 | **Install** | "Right when joining the app" needs no terminal | Signed and notarized macOS app, auto-update, then Windows/Linux |
| 8 | **Trust and abuse at public scale** | Free riders, Sybils, content | Contribution credits from receipts, memory-hard identity puzzles, reputation-driven cross-checks, an operator block list on relays, MCP sandboxing, external security review |
| 9 | **Relay cost** | All traffic is relayed | NAT hole punching with the relay as fallback (relays already do rendezvous). Optional for launch |
| 10 | **Scale > ~1k nodes** | Gossip views are bounded | DHT keyed by model and slice |

**Minimum for "users collectively run bigger models over the internet":** items 1, 2 and 3, plus
Qwen3 32B from item 4. Items 5–7 make it good. Items 8–10 make it survivable in public.

### 7.7 The Amazon path (research 2026-11-05; confirm each item with the owners named)

**This section is internal; strip it (and §6.5 item 8) from any public release.**

- **Ownership.** Mycel was built on a company laptop, so treat it as **Amazon code**. Personal
  side-project rules require no Amazon equipment. Don't store it outside Amazon (no personal
  GitHub) until the release is approved. An internal code repo is fine and gives you a backup.
- **Release process: SDO, because Leo is non-AWS.**
  1. A PR/FAQ or 1-pager, plus **L8 approval**, an **IP review** (licence defaults to
     Apache-2.0) and a **trademark review**. An abstract name like "Mycel" needs a naming exception.
  2. A private repo through the open-source tooling.
  3. Security, distribution and dependency reviews.
  4. Make it public.

  Mycel isn't tied to AWS, so it's likely a **"Community Project" (Type 3)**, which needs scoping
  approval from open-source program leadership and must pass a "value to Amazon" test. An AWS
  team adopting it (for example as an AWS-hosted accelerator) is the alternative path.
- **Export.** Leo-originated code needs a Kuiper trade-compliance classification. HPKE
  encryption adds export questions.
- **Models.** Every open-weight model needs a use and distribution approval.
  - **Llama is prohibited for production, with no exceptions.**
  - Qwen3 and gpt-oss (Apache-2.0) can be self-approved or ticketed. Gemma needs a ticket.
  - Approvals assume models run on **Amazon-controlled hosts**. A volunteer GPU mesh is a novel
    case, so expect legal to focus here.
  - Also raise who is responsible for prompts and outputs that pass through Amazon-run relays.
- **Relays on Amazon infrastructure.** An SDO AWS account. A public endpoint in an uncertified
  account is flagged High, so a full **Amazon Security Review** is needed first (6–8 weeks in
  Kuiper's process; no exception route). The deploy kit is already built to that shape: ALB, TLS
  1.3, WAF, no public IPs on tasks.
- **Promotion.** Comes after release, through the AWS Open Source blog (it needs a security
  content review; product pitches are declined). Promotional credits may be available. The L8
  approver is the first sponsor.

### 7.8 What you (the owner) need to do for users to join a global GPU mesh on day one

1. **Find a sponsor.** Talk to your manager, find an L8 sponsor, and write the PR/FAQ (an
   honest framing: an edge-AI / efficient-inference community project).
2. **Open the legal and compliance tickets in parallel:**
   - IP review.
   - Export classification.
   - Model use and distribution approvals, for Apache-2.0 models only: Qwen3, gpt-oss.
   - Trademark review, or pick a descriptive name.
3. **Get relays approved:**
   - An SDO AWS account and a domain.
   - Start the security review using the existing threat model (HPKE, PoW admission, relays see
     only ciphertext plus IPs).
4. **Before relays are public, run a two-machine trial.** This laptop still never hosts a
   relay. Ask the security contact whether a short-lived test account with the ALB ingress
   **limited to the trial machines' IPs** (not 0.0.0.0/0) is acceptable. If so, add an
   `allowedCidrs` context option to the CDK stack.
5. **Engineering in parallel with approvals:** §7.6 items 2 (as soon as a trial relay exists),
   3, 4 and 5, then 7.

### 7.9 Operating notes added this round

- **`mycel start` now defaults to internet mode.** The real node, when next restarted, will have
  no 7700 mesh listener and will show "No relays yet". Use `mycel start --local` for the old
  loopback mesh. Simulations and tests are unchanged: they pass explicit loopback relays and
  `internet` stays off.
- **Testing internet mode without leaving the machine.** Run
  `python3 -m mycel start --port 8100 --mesh-port 7990 --data-dir /tmp/x --no-open` (it opens
  only `127.0.0.1:8100`; confirm with `lsof -nP -iTCP -sTCP:LISTEN`). Don't add real relay URLs
  in tests. Use `.invalid` names with a stubbed `RelayTransport` (see `tests/test_internet.py`).
- **Synthesizing the CDK stack** without deploying: use a temporary virtualenv with
  `pip install -r deploy/relay/cdk/requirements.txt`, then run `python app.py` from
  `deploy/relay/cdk` with `CDK_OUTDIR=/tmp/out` and `CDK_CONTEXT_JSON` set to the context in
  `cdk.json`. Never run `cdk deploy` without explicit approval and an approved account.
- **No Amazon references in shipped code.** `deploy/relay/README.md` uses public AWS services
  only, by design. Keep Amazon-internal material in this doc only, and strip it before release.

---

## 8. Decision 2026-11-09: self-hosted relays (no AWS) and a libp2p networking layer

*Status: agreed direction. Nothing in this section is implemented yet.*

### 8.1 Decisions

1. **Infrastructure.** No AWS. Founding relays run on budget VPS providers: at least 2 providers
   and 2–3 regions, chosen for included bandwidth, because relayed swarm traffic is the main
   cost. `deploy/relay/cdk` is **superseded**: delete it once the replacement deploy kit exists
   (§8.6 L4).
2. **Networking.** Move Mycel's transport, NAT traversal and discovery onto **libp2p**. Keep
   Mycel's application layer: the planner, stage engines, bookend privacy, express lane,
   speculation, cross-checks, receipts, the governor, PoW identity, the app and the `/v1` API.

### 8.2 Assessment of the libp2p proposal

**Verdict: right direction, with these adjustments.**

| Proposal | Assessment | Adjustment |
|---|---|---|
| Kademlia DHT announcing which slices a node has | Yes: it scales past the current 2,000-peer gossip view (gap §7.6 #10). Petals does the same per block. | (a) Run a **private DHT** (protocol id `/mycel/kad/1.0.0`), never the public IPFS one. (b) Key by **fixed layer blocks** (`<model>/<quant>/block-<k>`, e.g. 4 layers per block), not by "slice N". Mycel's spans are flexible and move during rebalancing; a host announces every block it covers. (c) The DHT only says *who has* a block. The planner also needs live load, RTT and reliability, so keep **GossipSub topics per model** for fresh adverts, plus direct pings for RTT. |
| Stream multiplexing between consecutive stages | Yes. This is the express lane done properly: one long-lived stream per session between neighbouring stages, with no per-token handshakes. | Mycel already multiplexes over relay WebSockets. The gain is direct, multiplexed connections between stages once hole punching works. |
| No central control plane | Mycel already has none; relays are dumb forwarders. | No change. |
| AutoNAT + hole punching | Yes. This is the biggest missing piece (gap §7.6 #9) and the key to relay cost and to self-sustainability. | Hole punching is libp2p **DCUtR** (relay-coordinated), not STUN. Published measurements put success at **roughly 70%**, so about 30% of pairs (symmetric NATs, strict firewalls) still need a relay. Budget relay bandwidth for that share. |
| Circuit Relay v2, with a few high-bandwidth project relays early on | Yes. Relayed traffic stays Noise-encrypted end to end, so relays still see only ciphertext. | Relay v2's default limits are small, intended for coordinating hole punches (go-libp2p: ~2 min and ~128 KiB per relayed connection). **Project and volunteer "data relays" must run with raised limits**, while ordinary public nodes keep the defaults. Size relays for the ~30% that can't punch through. |
| QUIC as primary transport | Yes: faster handshakes, better on lossy home links, and it hole-punches better than TCP. | Some corporate and hotel networks block UDP. Keep **TCP and WebSocket-over-443** as fallback transports. |
| Protobuf or FlatBuffers for activations | Right goal: today activations are base64 JSON (gap §7.6 #5). | Use protobuf for **control messages** only. Send tensors as a **fixed binary header plus raw fp16/bf16 bytes**; a schema adds nothing for a flat tensor. |
| Petals-style scheduling, quantization and batching against latency | Yes. | Mycel already has a latency-aware planner (Dijkstra over layer boundaries), an express lane and exact speculative decoding. The missing pieces are the **4-bit stage engine** (§7.6 #3) and **request batching on stages** (new). |
| Verification by consensus checks or ZK proofs | Consensus checks: yes, Mycel already does random cross-checks with a tie-breaker. | **Not ZK.** Proofs of LLM execution are orders of magnitude too slow today. Instead: reputation-driven cross-check rates, signed receipts, and evicting nodes caught lying (already built). |

### 8.3 Answers to the notes' two questions

- **Language.** The core stays **Python** (app, planner, engines). Networking moves to a **native
  libp2p sidecar** (`mycel-net`) that the Python node drives over a local socket. This is the
  pattern hivemind/Petals use with go-libp2p-daemon.
  - **Go (go-libp2p) is recommended.** It's the reference implementation with the most
    battle-tested QUIC, AutoNAT v2, relay v2, DCUtR, Kad-DHT and GossipSub, and has a proven
    Python sidecar precedent.
  - **Rust (rust-libp2p)** is an equally capable alternative if you'd rather ship one native
    binary that later also holds the ggml stage engine.
  - **py-libp2p is not recommended for production.** Its QUIC and NAT-traversal support lags
    the Go and Rust implementations.
  - The sidecar is bundled in the app, so users never see it.
- **Target model size.**
  - **First:** Qwen3 32B (dense, Apache-2.0) across 3–4 consumer GPUs with 8–16 GB each.
  - **Then:** MoE giants, gpt-oss-120b and Qwen3-235B-A22B (both Apache-2.0). These need MoE
    swarm support (§7.6 #4).
  - **Not Llama.** It is prohibited for Amazon production use (§7.7).

### 8.4 Target architecture

```
 Python node (unchanged above the Transport interface)
   planner · swarm client/host · engines · governor · receipts · app · /v1
        │  local socket (stream open/accept, DHT provide/find, pubsub)
 mycel-net sidecar (go-libp2p)
   identity: the node's existing Ed25519 key → libp2p peer id (same identity; PoW ticket unchanged)
   transports: QUIC (primary) · TCP · WebSocket/443 (fallback)
   security/muxing: Noise + yamux (QUIC has built-in muxing)
   reachability: AutoNAT v2 · DCUtR hole punching · Circuit Relay v2 client
   discovery: private Kad-DHT (block provider records) · GossipSub per-model topics
   relay service: off by default; on (raised limits) for project and volunteer data relays
```

- **Identity continuity.** Mycel node ids are already Ed25519, so the libp2p peer id derives
  from the same key. Membership proofs, receipts and blocking carry over unchanged.
- **Encryption.** Noise gives authenticated, end-to-end encryption per connection, including
  over circuit relays. Keep the HPKE request envelope during migration. Drop it from hot paths
  (activation streams) once the libp2p path is proven, but keep signed envelopes for receipts
  and adverts.
- **Bootstrap.** Seed peers as `/dnsaddr/bootstrap.<domain>` multiaddrs (DNS TXT records), plus
  a bundled fallback list. The seed list can then change without an app release. Seed nodes are
  DHT servers plus relays with raised limits.
- **Self-sustaining.** Founding seeds are needed only for first contact. Publicly reachable
  members can become DHT servers automatically when AutoNAT says they're reachable. Volunteers
  opt into data relaying, which counts toward contribution credits (§7.6 #8).

### 8.5 How a new user joins (target)

1. Install the signed app (the sidecar is bundled) and tap **Join**: identity and PoW ticket.
2. The sidecar dials the seed peers over QUIC (TCP/WSS fallback) and learns its reachability via
   AutoNAT. If it's behind NAT, it reserves a slot on 1–2 relays.
3. It joins the DHT, subscribes to the GossipSub topics for the models in the picker, and
   announces its own model or slice blocks.
4. Chat on a swarm model: query the DHT for each block's providers, read live load from gossip,
   ping candidates, plan the chain, then open streams to each stage (hole-punched direct where
   possible, relayed otherwise), with an express-lane stream between consecutive stages.

### 8.6 Migration plan (each step keeps the 62 tests green)

| Step | Work | Acceptance |
|---|---|---|
| **L0 spike** | Minimal go-libp2p sidecar: QUIC/TCP/WSS, Noise, yamux, AutoNAT v2, relay v2 client and server, DCUtR, Kad-DHT, GossipSub, and a stream API over a local socket. One seed/relay on a VPS. | Two machines on different home networks: direct (hole-punched) connection rate and latency measured, relayed fallback works, round-trip of a 16 KB tensor reported. |
| **L1 transport** | `Libp2pTransport` behind Mycel's existing transport interface. Simulations get a loopback mode so `mycel simulate` and the tests can run either transport. | The full test suite passes on both transports. `relay_harness`-style run: ≥ 97% routes OK with churn. |
| **L2 discovery** | DHT block provider records (republished every ~10 min, ~30 min TTL) and per-model GossipSub adverts. The planner consumes them. | 1k simulated nodes: time to a first swarm plan ≤ 3 s, and no 2,000-peer view limit. |
| **L3 data path** | Persistent binary activation streams between consecutive stages (header plus raw fp16), with protobuf control. | Real-weights swarm-demo over the sidecar matches today's outputs token for token. Per-hop overhead well below today's base64 JSON. |
| **L4 relays** | Deploy kit for seed and relay nodes: one static sidecar binary plus systemd unit, a firewall script, and DNS TXT bootstrap records. Remove `deploy/relay/cdk`. 2–3 nodes on 2 providers. | Two-network join-and-chat in < 60 s with no inbound ports. A relay can be killed without users noticing. |
| **L5 real swarm** | With the 4-bit stage engine (§7.6 #3): Qwen3 32B across 3–4 Macs on different networks. | Measured tok/s and first-token time published honestly; the reply survives killing a stage. |

**Running in parallel:** the 4-bit stage engine, stage request batching, MoE swarm support, and
the signed installer that bundles the sidecar.

### 8.7 Risks and open questions

- **Rewrite risk.** The transport layer is replaced, but the interface boundary is kept, so the
  app and swarm code above it don't change. The current WebSocket relay path stays as a
  fallback until L4 passes.
- **Relay bandwidth.** About 30% of pairs can't hole-punch and must be relayed. Measure the
  real rate in L0 before sizing providers.
- **DHT abuse.** Sybil and eclipse attacks on provider records. Mitigations: provider records
  only from ticketed peers (verify the PoW ticket on lookup), disjoint lookup paths, and the
  existing cross-checks catching bad stages.
- **UDP-hostile networks.** The WSS-over-443 fallback must stay first-class.
- **Licence check.** go-libp2p and rust-libp2p are MIT/Apache-2.0 licensed. Include them in the
  release's dependency and distribution review.

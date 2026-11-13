"""Model library: a curated catalog every node shares, and a supervisor that serves one locally.

Every node serving ``qwen3-8b`` runs the *same* weights (same Hugging Face repo, same quant),
so a model name means the same thing everywhere on the mesh — the model you chat with is the
model you serve.

Serving runs llama.cpp's ``llama-server`` as a child process bound to 127.0.0.1 on a free port.
It downloads weights from Hugging Face on first use (``-hf repo:quant``) into the Mycel data
folder. The node then fronts it; the model server itself is never reachable from the mesh.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import aiohttp

log = logging.getLogger("mycel")

KV_OVERHEAD_GB = 1.5        # KV cache + buffers at the default 8k context
LOAD_FACTOR = 1.1           # runtime overhead over the file size
DECODE_EFFICIENCY = 0.7     # fraction of memory bandwidth a decode step achieves in practice
REASONING_BUDGET = 768      # max thinking tokens before a reasoning model must answer


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    family: str
    params_b: float           # total parameters (billions)
    active_b: float           # parameters touched per token (== params for dense models)
    file: str                 # GGUF file name (for detecting a finished download)
    hf: str                   # "<repo>:<quant>" passed to llama-server -hf
    size_gb: float            # exact file size from the Hugging Face API
    capability: int           # 1..5 relative guide (not a benchmark score)
    license: str
    blurb: str
    # Architecture facts used by swarm (split) inference. arch "" = can't be split yet.
    arch: str = ""
    layers: int = 0
    hidden: int = 0
    vocab: int = 0
    tied: bool = False
    draft: str | None = None   # a small same-tokenizer model for speculative decoding

    @property
    def memory_gb(self) -> float:
        return round(self.size_gb * LOAD_FACTOR + KV_OVERHEAD_GB, 1)

    @property
    def moe(self) -> bool:
        return self.active_b < self.params_b

    @property
    def swarm(self) -> bool:
        return self.arch in ("llama", "qwen3") and self.layers > 0

    @property
    def bookend_gb(self) -> float:
        """Embedding + LM head, held by the requester in swarm mode."""
        bytes_per_param = self.size_gb / self.params_b   # GB per billion params
        return round(self.vocab * self.hidden * (1 if self.tied else 2) / 1e9 * bytes_per_param, 2)

    @property
    def layer_gb(self) -> float:
        """Memory one transformer layer needs on a stage (weights + runtime overhead)."""
        return round((self.size_gb - self.bookend_gb) / self.layers * LOAD_FACTOR, 3) if self.layers else 0.0

    def layer_gb_for(self, engine: str) -> float:
        """Memory per layer on a given stage engine: quantized (simulated ggml), fp16 (GPU) or fp32 (CPU)."""
        if engine == "mock" or not self.layers:
            return self.layer_gb
        per_layer = (self.params_b * 1e9 - self.vocab * self.hidden * (1 if self.tied else 2)) / self.layers
        return round(per_layer * (2 if engine == "torch" else 4) * 1.05 / 1e9, 3)

    def est_layer_ms(self, bandwidth_gbs: float) -> float:
        """Decode time per token per layer: memory-bound, one pass over that layer's weights."""
        if bandwidth_gbs <= 0 or not self.layers:
            return 0.0
        return round(self.layer_gb / LOAD_FACTOR / (bandwidth_gbs * DECODE_EFFICIENCY) * 1000, 3)

    def est_tokens_per_s(self, bandwidth_gbs: float) -> float | None:
        if bandwidth_gbs <= 0:
            return None
        bytes_per_token_gb = self.size_gb * (self.active_b / self.params_b)
        return round(DECODE_EFFICIENCY * bandwidth_gbs / bytes_per_token_gb, 0)


# Sizes verified against the Hugging Face API (2026-11). Capability is a coarse relative guide.
CATALOG = [
    CatalogModel("gpt-oss-120b", "gpt-oss 120B", "OpenAI gpt-oss", 117, 5.1, "gpt-oss-120b-MXFP4.gguf",
                 "ggml-org/gpt-oss-120b-GGUF", 63.39, 5, "Apache-2.0", "The most capable open model here. Mixture-of-experts, so it's fast for its size."),
    CatalogModel("llama-3.3-70b", "Llama 3.3 70B", "Meta Llama", 70.6, 70.6, "Llama-3.3-70B-Instruct-Q4_K_M.gguf",
                 "bartowski/Llama-3.3-70B-Instruct-GGUF:Q4_K_M", 42.52, 4, "Llama 3.3 Community", "Large dense generalist. Strong writing and knowledge.",
                 arch="llama", layers=80, hidden=8192, vocab=128256, draft="llama-3.2-3b"),
    CatalogModel("qwen3-32b", "Qwen3 32B", "Qwen3", 32.8, 32.8, "Qwen3-32B-Q4_K_M.gguf",
                 "Qwen/Qwen3-32B-GGUF:Q4_K_M", 19.76, 4, "Apache-2.0", "Excellent reasoning and coding for its size.",
                 arch="qwen3", layers=64, hidden=5120, vocab=151936, draft="qwen3-1.7b"),
    CatalogModel("qwen3-30b-a3b", "Qwen3 30B-A3B", "Qwen3", 30.5, 3.3, "Qwen3-30B-A3B-Q4_K_M.gguf",
                 "Qwen/Qwen3-30B-A3B-GGUF:Q4_K_M", 18.56, 4, "Apache-2.0", "Mixture-of-experts: big-model quality at small-model speed."),
    CatalogModel("gemma-3-27b", "Gemma 3 27B", "Google Gemma", 27.0, 27.0, "gemma-3-27b-it-Q4_K_M.gguf",
                 "ggml-org/gemma-3-27b-it-GGUF:Q4_K_M", 16.55, 4, "Gemma Terms", "Well-rounded, multilingual, great at conversation."),
    CatalogModel("gpt-oss-20b", "gpt-oss 20B", "OpenAI gpt-oss", 21.0, 3.6, "gpt-oss-20b-MXFP4.gguf",
                 "ggml-org/gpt-oss-20b-GGUF", 12.11, 3, "Apache-2.0", "Fast mixture-of-experts reasoner that fits a 16 GB machine."),
    CatalogModel("qwen3-14b", "Qwen3 14B", "Qwen3", 14.8, 14.8, "Qwen3-14B-Q4_K_M.gguf",
                 "Qwen/Qwen3-14B-GGUF:Q4_K_M", 9.00, 3, "Apache-2.0", "Capable everyday model for 16 GB GPUs and laptops.",
                 arch="qwen3", layers=40, hidden=5120, vocab=151936, draft="qwen3-1.7b"),
    CatalogModel("gemma-3-12b", "Gemma 3 12B", "Google Gemma", 12.0, 12.0, "gemma-3-12b-it-Q4_K_M.gguf",
                 "ggml-org/gemma-3-12b-it-GGUF:Q4_K_M", 7.30, 3, "Gemma Terms", "Friendly and fluent; a good default on 12 GB cards."),
    CatalogModel("qwen3-8b", "Qwen3 8B", "Qwen3", 8.2, 8.2, "Qwen3-8B-Q4_K_M.gguf",
                 "Qwen/Qwen3-8B-GGUF:Q4_K_M", 5.03, 3, "Apache-2.0", "The sweet spot for 8 GB GPUs.",
                 arch="qwen3", layers=36, hidden=4096, vocab=151936, draft="qwen3-1.7b"),
    CatalogModel("qwen3-4b", "Qwen3 4B", "Qwen3", 4.0, 4.0, "Qwen3-4B-Q4_K_M.gguf",
                 "Qwen/Qwen3-4B-GGUF:Q4_K_M", 2.50, 2, "Apache-2.0", "Small and quick; surprisingly good reasoning.",
                 arch="qwen3", layers=36, hidden=2560, vocab=151936, tied=True, draft="qwen3-1.7b"),
    CatalogModel("gemma-3-4b", "Gemma 3 4B", "Google Gemma", 4.3, 4.3, "gemma-3-4b-it-Q4_K_M.gguf",
                 "ggml-org/gemma-3-4b-it-GGUF:Q4_K_M", 2.49, 2, "Gemma Terms", "Small, fluent conversationalist."),
    CatalogModel("llama-3.2-3b", "Llama 3.2 3B", "Meta Llama", 3.2, 3.2, "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
                 "bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M", 2.02, 2, "Llama 3.2 Community", "Lightweight helper for modest machines.",
                 arch="llama", layers=28, hidden=3072, vocab=128256, tied=True),
    CatalogModel("qwen3-1.7b", "Qwen3 1.7B", "Qwen3", 1.7, 1.7, "Qwen3-1.7B-Q8_0.gguf",
                 "Qwen/Qwen3-1.7B-GGUF:Q8_0", 1.83, 1, "Apache-2.0", "Tiny and instant. Good for quick tasks and testing.",
                 arch="qwen3", layers=28, hidden=2048, vocab=151936, tied=True),
]
BY_ID = {m.id: m for m in CATALOG}


def register(m: CatalogModel) -> None:
    """Add a model at runtime (tests and demos with tiny local models)."""
    if m.id not in BY_ID:
        CATALOG.append(m)
    BY_ID[m.id] = m


def capability(model_id: str) -> int:
    m = BY_ID.get(model_id)
    return m.capability if m else 0


def describe(model_id: str, accel_gb: float, bandwidth_gbs: float) -> dict:
    m = BY_ID[model_id]
    fits = accel_gb >= m.memory_gb
    return {**asdict(m), "memory_gb": m.memory_gb, "moe": m.moe, "fits_device": fits,
            "est_tokens_per_s": m.est_tokens_per_s(bandwidth_gbs) if fits else None}


MIN_SERVE_TPS = 8.0


def recommend(accel_gb: float, bandwidth_gbs: float = 0.0) -> str | None:
    """Most capable model that fits comfortably (~20% headroom for the user's own apps) and is
    fast enough to serve; among equally capable ones, the fastest."""
    best = None
    for m in CATALOG:  # ordered most -> least capable
        if m.memory_gb > accel_gb * 0.8:
            continue
        tps = m.est_tokens_per_s(bandwidth_gbs)
        if tps is not None and tps < MIN_SERVE_TPS:
            continue
        if best is None or (m.capability == best[0].capability and (tps or 0) > (best[1] or 0)):
            best = (m, tps)
        elif m.capability < best[0].capability:
            break
    return best[0].id if best else None


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ModelServer:
    """Supervises one llama-server child process (or a test double with the same CLI)."""

    def __init__(self, models_dir: Path, server_cmd: list[str] | None = None):
        self.models_dir = Path(models_dir)
        self.server_cmd = server_cmd or ([shutil.which("llama-server")] if shutil.which("llama-server") else None)
        self.proc: asyncio.subprocess.Process | None = None
        self.model_id: str | None = None
        self.port: int | None = None
        self.status = "idle"            # idle | downloading | loading | ready | error
        self.error: str | None = None
        self.started_at: float | None = None

    @property
    def available(self) -> bool:
        return self.server_cmd is not None

    @property
    def base_url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def _repo_dir(self, model_id: str) -> Path:
        repo = BY_ID[model_id].hf.split(":")[0]
        return self.models_dir / ("models--" + repo.replace("/", "--"))

    def downloaded(self, model_id: str) -> bool:
        """Finished download in either llama.cpp cache layout (flat, or Hugging Face snapshots/)."""
        m = BY_ID[model_id]
        if not self.models_dir.exists():
            return False
        return any(p.name.endswith(m.file) and p.exists() for p in self.models_dir.rglob(f"*{m.file}"))

    def progress(self) -> float | None:
        """Download progress 0..1, estimated from the partial file on disk."""
        if self.status != "downloading" or not self.model_id:
            return None
        m = BY_ID[self.model_id]
        partial = list(self._repo_dir(self.model_id).glob("blobs/*.downloadInProgress"))
        partial += [p for p in self.models_dir.glob("*") if m.file in p.name and p.suffix != ".gguf"]
        size = max((p.stat().st_size for p in partial), default=0)
        return min(0.99, size / (m.size_gb * 1e9))

    @property
    def log_path(self) -> Path:
        return self.models_dir / "llama-server.log"

    def _log_tail(self, n: int = 4) -> str:
        """Last meaningful lines of the model server's log (for error messages)."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 16384))
                text = f.read().decode(errors="replace")
        except OSError:
            return ""
        lines = [l.strip() for l in text.replace("\r", "\n").splitlines() if l.strip() and "%" not in l]
        return " | ".join(lines[-n:])[:600]

    async def start(self, model_id: str, ctx: int = 8192) -> None:
        if not self.available:
            raise RuntimeError("llama-server isn't installed. Install llama.cpp (e.g. `brew install llama.cpp`).")
        await self.stop()
        m = BY_ID[model_id]
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.model_id, self.port, self.error = model_id, _free_port(), None
        self.status = "loading" if self.downloaded(model_id) else "downloading"
        env = {**os.environ, "LLAMA_CACHE": str(self.models_dir)}
        args = [*self.server_cmd, "-hf", m.hf, "--host", "127.0.0.1", "--port", str(self.port),
                "--alias", m.id, "--jinja", "-ngl", "999", "-c", str(ctx),
                # Cap visible thinking so reasoning models always leave room to answer.
                "--reasoning-budget", str(REASONING_BUDGET)]
        # Output goes to a log file, never a pipe: download progress bars write long lines without
        # newlines, and a stalled or closed pipe would block or kill the server.
        log = open(self.log_path, "ab")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} serving {m.id} =====\n".encode())
        log.flush()
        try:
            self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                             stdout=log, stderr=log, env=env)
        finally:
            log.close()  # the child keeps its own handle
        self.started_at = time.time()
        self._pid_path.write_text(str(self.proc.pid))

    @property
    def _pid_path(self) -> Path:
        return self.models_dir / "llama-server.pid"

    def reap_stale(self) -> None:
        """Stop a model server left behind by a crashed node (only if it's really ours)."""
        try:
            pid = int(self._pid_path.read_text())
        except (OSError, ValueError):
            return
        try:
            import subprocess
            cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            cmd = ""
        if "llama-server" in cmd and f"LLAMA_CACHE" not in cmd and "--alias" in cmd:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, 15)
                log.info("stopped a leftover model server (pid %s)", pid)
        self._pid_path.unlink(missing_ok=True)

    async def wait_ready(self, timeout_s: float = 3600) -> None:
        """Wait until the server reports healthy (covers download + load)."""
        deadline = time.monotonic() + timeout_s
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            while time.monotonic() < deadline:
                if self.proc is None or self.proc.returncode is not None:
                    self.status = "error"
                    self.error = "model server exited: " + (self._log_tail() or "no output")
                    raise RuntimeError(self.error)
                if self.status == "downloading" and self.downloaded(self.model_id):
                    self.status = "loading"
                try:
                    async with s.get(f"{self.base_url}/health") as r:
                        if r.status == 200:
                            self.status = "ready"
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(1)
        self.status, self.error = "error", "timed out waiting for the model server"
        raise RuntimeError(self.error)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.proc, self.port, self.status = None, None, "idle"
        self._pid_path.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        return {"model_id": self.model_id, "status": self.status, "error": self.error,
                "progress": self.progress(), "llama_server": self.available}

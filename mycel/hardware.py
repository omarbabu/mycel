"""Hardware detection and the minimum bar for serving on the mesh.

The source of truth is the engine that will actually run the model: ``llama-server
--list-devices`` reports every accelerator llama.cpp can use (Metal, CUDA, ROCm, Vulkan, SYCL)
with its usable memory — no PyTorch needed. Memory bandwidth, which decides decode speed,
comes from a table of common chips and is replaced by a back-solved number once a model is
actually served and its real tokens/s are measured.

Minimum to SERVE (anyone can join and chat without a GPU):

* a GPU llama.cpp can drive: Apple silicon (Metal), NVIDIA (CUDA), AMD (ROCm or Vulkan), Intel (SYCL/Vulkan)
* >= 8 GB usable accelerator memory (Apple silicon: what Metal lets one process wire, ~70-75% of RAM)
* the model it serves must sustain >= 8 tokens/s (measured when serving starts)

Swarm hosting (holding a slice of a model too big for one GPU) needs the same GPU but only
as much memory as the slice — an 8 GB card can hold ~14 layers of a 70B model.

Peers can lie about any of this, so routers weight *observed* throughput over claims.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

import psutil

MIN_MEMORY_GB = 8.0
MIN_TOKENS_PER_S = 8.0
GPU_KINDS = ("mps", "cuda", "rocm", "vulkan", "sycl")

TIERS = [  # (name, min accelerator GB, what fits at 4-bit)
    ("T3", 48.0, "70B-class"),
    ("T2", 16.0, "up to ~32B"),
    ("T1", MIN_MEMORY_GB, "7-8B class"),
]

# llama.cpp backend prefix -> accelerator kind, in order of preference when a build lists the
# same GPU under several backends (e.g. CUDA and Vulkan).
_BACKENDS = [("MTL", "mps"), ("CUDA", "cuda"), ("ROCm", "rocm"), ("HIP", "rocm"), ("SYCL", "sycl"), ("Vulkan", "vulkan")]
_DEVICE_LINE = re.compile(r"^\s*(?P<id>[A-Za-z]+)(?P<idx>\d*):\s*(?P<name>.*?)\s*\((?P<total>\d+)\s*MiB,\s*(?P<free>\d+)\s*MiB free\)\s*$")

# Peak memory bandwidth (GB/s) of common chips. Longest/most specific patterns first.
BANDWIDTH_TABLE: list[tuple[str, float]] = [
    # Apple silicon
    (r"M1 Ultra", 800), (r"M1 Max", 400), (r"M1 Pro", 200), (r"M1\b", 68),
    (r"M2 Ultra", 800), (r"M2 Max", 400), (r"M2 Pro", 200), (r"M2\b", 100),
    (r"M3 Ultra", 819), (r"M3 Max", 400), (r"M3 Pro", 150), (r"M3\b", 100),
    (r"M4 Max", 546), (r"M4 Pro", 273), (r"M4\b", 120), (r"M5\b", 153),
    # NVIDIA GeForce
    (r"5090", 1792), (r"5080", 960), (r"5070 Ti", 896), (r"5070", 672), (r"5060 Ti", 448), (r"5060", 448),
    (r"4090", 1008), (r"4080 SUPER", 736), (r"4080", 717), (r"4070 Ti SUPER", 672), (r"4070 Ti", 504),
    (r"4070 SUPER", 504), (r"4070", 504), (r"4060 Ti", 288), (r"4060", 272),
    (r"3090 Ti", 1008), (r"3090", 936), (r"3080 Ti", 912), (r"3080", 760), (r"3070 Ti", 608), (r"3070", 448),
    (r"3060 Ti", 448), (r"3060", 360), (r"3050", 224),
    (r"2080 Ti", 616), (r"2080", 448), (r"2070", 448), (r"2060", 336),
    (r"1660 SUPER", 336), (r"1660 Ti", 288), (r"1660", 192), (r"1080 Ti", 484), (r"1080", 320), (r"1070", 256),
    # NVIDIA data centre / workstation
    (r"H200", 4800), (r"H100", 3350), (r"A100.*80", 2039), (r"A100", 1555), (r"L40S", 864), (r"L40", 864),
    (r"RTX 6000 Ada", 960), (r"A6000", 768), (r"A5000", 768), (r"A4000", 448), (r"A10G?\b", 600),
    (r"L4\b", 300), (r"T4\b", 320), (r"V100", 900),
    # AMD
    (r"MI300X", 5300), (r"MI250", 3277), (r"9070 XT", 640), (r"9070", 640),
    (r"7900 XTX", 960), (r"7900 XT", 800), (r"7900 GRE", 576), (r"7800 XT", 624), (r"7700 XT", 432), (r"7600", 288),
    (r"6950 XT", 576), (r"6900 XT", 512), (r"6800 XT", 512), (r"6800", 512), (r"6700 XT", 384), (r"6600", 224),
    # Intel
    (r"Arc.*A770", 560), (r"Arc.*A750", 512), (r"Arc.*B580", 456),
]


@dataclass
class HardwareProfile:
    accelerator: str          # "mps" | "cuda" | "rocm" | "vulkan" | "sycl" | "cpu"
    device_name: str
    accel_memory_gb: float    # usable accelerator memory, summed over devices
    tflops_fp16: float = 0.0  # optional (PyTorch probe only); not used for eligibility
    system_memory_gb: float = 0.0
    bandwidth_gbs: float = 0.0     # memory bandwidth that drives speed estimates
    simulated: bool = False
    devices: list = field(default_factory=list)   # [{"id", "name", "memory_gb", "free_gb"}]
    source: str = "none"           # "llama.cpp" | "torch" | "simulated" | "none"
    bandwidth_source: str = "unknown"   # "table" | "measured" | "probe" | "unknown"

    @property
    def is_gpu(self) -> bool:
        return self.accelerator in GPU_KINDS

    @property
    def tier(self) -> str:
        if not meets_hardware_minimum(self):
            return "relay"
        for name, gb, _ in TIERS:
            if self.accel_memory_gb >= gb:
                return name
        return "relay"

    def to_dict(self) -> dict:
        return {**asdict(self), "tier": self.tier}


def meets_hardware_minimum(hw: HardwareProfile) -> bool:
    return hw.is_gpu and hw.accel_memory_gb >= MIN_MEMORY_GB


def check_serving_eligibility(hw: HardwareProfile, backend_tps: float) -> list[str]:
    """Return a list of human-readable reasons this node can't serve (empty = eligible)."""
    problems = []
    if not hw.is_gpu:
        problems.append("no supported GPU (Apple Metal, CUDA, ROCm or Vulkan) detected")
    if hw.accel_memory_gb < MIN_MEMORY_GB:
        problems.append(f"{hw.accel_memory_gb:.1f} GB accelerator memory < {MIN_MEMORY_GB:.0f} GB minimum")
    if backend_tps < MIN_TOKENS_PER_S:
        problems.append(f"backend measured {backend_tps:.1f} tok/s < {MIN_TOKENS_PER_S:.0f} tok/s minimum")
    return problems


def bandwidth_for(name: str) -> float:
    """Peak memory bandwidth for a known chip, or 0.0 if unknown."""
    for pattern, gbs in BANDWIDTH_TABLE:
        if re.search(pattern, name, re.IGNORECASE):
            return float(gbs)
    return 0.0


def parse_list_devices(text: str) -> list[dict]:
    """Parse ``llama-server --list-devices`` output into GPU devices of the preferred backend.

    Lines look like ``  MTL0: Apple M3 Pro (28753 MiB, 28753 MiB free)``. CPU/BLAS entries are
    ignored; if one build lists the same GPUs under several backends, only the best backend
    (Metal > CUDA > ROCm > SYCL > Vulkan) is kept so memory isn't double-counted.
    """
    found: dict[str, list[dict]] = {}
    for line in text.splitlines():
        m = _DEVICE_LINE.match(line)
        if not m:
            continue
        kind = next((k for prefix, k in _BACKENDS if m["id"].lower() == prefix.lower()), None)
        if kind is None:
            continue
        total, free = int(m["total"]) / 1024, int(m["free"]) / 1024
        if total <= 0:
            continue
        found.setdefault(kind, []).append({"id": f"{m['id']}{m['idx']}", "kind": kind, "name": m["name"].strip(),
                                           "memory_gb": round(total, 1), "free_gb": round(free, 1)})
    for _, kind in _BACKENDS:
        if kind in found:
            return found[kind]
    return []


def profile_from_devices(devices: list[dict], system_gb: float, cpu_name: str = "") -> HardwareProfile:
    if not devices:
        return HardwareProfile("cpu", cpu_name or platform.processor() or "CPU", 0.0, 0.0, round(system_gb, 1), source="llama.cpp")
    kind = devices[0]["kind"]
    names = [d["name"] for d in devices]
    name = names[0] if len(set(names)) == 1 and len(names) == 1 else (
        f"{len(names)}x {names[0]}" if len(set(names)) == 1 else " + ".join(names))
    if kind == "mps" and not name.endswith("GPU"):
        name = f"{name} GPU"
    # Multi-GPU: llama.cpp splits layers across cards, so decode speed is bounded by one card.
    bws = [bandwidth_for(d["name"]) for d in devices]
    bw = min(bws) if all(bws) else 0.0
    return HardwareProfile(kind, name, round(sum(d["memory_gb"] for d in devices), 1), 0.0, round(system_gb, 1),
                           bw, devices=[{k: d[k] for k in ("id", "name", "memory_gb", "free_gb")} for d in devices],
                           source="llama.cpp", bandwidth_source="table" if bw else "unknown")


def _cpu_name() -> str:
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2).stdout
            return out.strip()
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or "CPU"


def probe(server_cmd: list[str] | None = None, timeout_s: float = 30.0) -> HardwareProfile:
    """Detect accelerators via llama.cpp; fall back to an optional PyTorch probe, then CPU-only."""
    sys_gb = psutil.virtual_memory().total / 2**30
    cmd = server_cmd or ([shutil.which("llama-server")] if shutil.which("llama-server") else None)
    if cmd:
        try:
            out = subprocess.run([*cmd, "--list-devices"], capture_output=True, text=True, timeout=timeout_s)
            devices = parse_list_devices(out.stdout + "\n" + out.stderr)
            if devices or out.returncode == 0:
                return profile_from_devices(devices, sys_gb, _cpu_name())
        except (OSError, subprocess.SubprocessError):
            pass
    torch_hw = _probe_torch(sys_gb)
    if torch_hw is not None:
        return torch_hw
    return HardwareProfile("cpu", _cpu_name(), 0.0, 0.0, round(sys_gb, 1), source="none")


def _probe_torch(sys_gb: float) -> HardwareProfile | None:
    """Optional extra probe (``pip install mycel[probe]``): only used when llama.cpp is absent."""
    try:
        import time

        import torch
    except ImportError:
        return None
    if torch.cuda.is_available():
        dev, props = torch.device("cuda"), torch.cuda.get_device_properties(0)
        name, mem_gb, sync, kind = props.name, props.total_memory / 2**30, torch.cuda.synchronize, "cuda"
    elif torch.backends.mps.is_available():
        dev, name, kind, sync = torch.device("mps"), f"{_cpu_name() or 'Apple silicon'} GPU", "mps", torch.mps.synchronize
        try:
            mem_gb = torch.mps.recommended_max_memory() / 2**30
        except AttributeError:
            mem_gb = sys_gb * 0.7
    else:
        return None
    src = torch.empty(128 * 2**20, device=dev, dtype=torch.float16)
    dst = torch.empty_like(src)
    dst.copy_(src)
    sync()
    reps, t0 = 20, time.perf_counter()
    for _ in range(reps):
        dst.copy_(src)
    sync()
    bw = 2 * src.numel() * src.element_size() * reps / (time.perf_counter() - t0) / 1e9
    return HardwareProfile(kind, name, round(mem_gb, 1), 0.0, round(sys_gb, 1), round(bw, 1), source="torch",
                           bandwidth_source="probe")

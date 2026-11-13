"""Node configuration (TOML). Every default is the private/conservative choice."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .governor import Limits
from .location import PRECISIONS
from .netguard import validate_mesh_bind


@dataclass
class MeshConfig:
    bind_host: str = "127.0.0.1"
    port: int = 7700
    advertise_addr: str | None = None      # host:port peers should dial; defaults to bind_host:port
    allow_non_loopback: bool = False
    bootstrap: list[str] = field(default_factory=list)   # extra peers, "<node-id>@<ip>:<port>"
    global_bootstrap: list[str] = field(default_factory=list)  # seed peers of the global mesh
    auto_join_global: bool = False   # headless nodes: join the global mesh at startup
    listen: bool = True               # False = no inbound listener at all (behind NAT; relays only)
    relays: list[str] = field(default_factory=list)   # outbound relay URLs (ws:// loopback in simulations, wss:// otherwise)
    # Internet mode: reach the mesh only through public wss:// relays (bundled seeds, your own, and
    # ones learned from peers). Requires listen = false: the node opens no port at all. Relays are
    # contacted only after you join a mesh.
    internet: bool = False
    gossip_interval_s: float = 2.0
    gossip_fanout: int = 3
    advert_ttl_s: float = 30.0


@dataclass
class LocationConfig:
    share: bool = False
    lat: float | None = None
    lon: float | None = None
    precision: str = "region"


@dataclass
class VisibilityConfig:
    """What other members can see about this node beyond what routing needs. All off by default."""
    device: bool = False      # GPU / chip model, exact memory and bandwidth (otherwise only a coarse tier)
    activity: bool = False    # how many requests this node has served


@dataclass
class SwarmConfig:
    """Hosting a slice (a range of layers) of a model too big for one GPU."""
    model: str | None = None          # catalog id of the model to host a slice of (None = not hosting)
    budget_gb: float = 0.0            # accelerator memory to give the slice (0 = automatic)
    engine: str = "auto"              # "auto" | "torch" (GPU, real weights) | "gguf" (CPU NumPy, real weights) | "mock" (simulations)
    layers: int = 0                   # how many layers to host (0 = as many as fit in the budget)
    weights_url: str = "https://huggingface.co"   # where slices are fetched from (tests point this at loopback)
    files: dict = field(default_factory=dict)   # model id -> local GGUF path (weights already on disk)
    max_sessions: int = 4             # concurrent chats this node computes layers for
    rebalance_s: float = 30.0         # how often to reconsider which layers to host
    draft: bool = True                # use a small local draft model for speculative decoding when available
    crosscheck: bool = True           # re-run one stage per reply on an independent replica and compare
    express: bool = True              # decode steps flow stage-to-stage (N+1 network legs per token, not 2N)


@dataclass
class BackendConfig:
    kind: str = "managed"       # "managed" (Mycel runs llama-server for the model you pick) | "openai" | "mock" | "none"
    base_url: str = "http://127.0.0.1:8080"
    models: list[str] = field(default_factory=list)
    api_key_env: str | None = None
    mock_tokens_per_s: float = 40.0
    server_cmd: list[str] | None = None   # override the llama-server command (tests)


@dataclass
class NodeConfig:
    name: str = "node"
    data_dir: str = "~/.mycel"
    control_port: int = 7800
    serve: bool = True
    probe_hardware: bool = True
    simulated_hardware: dict | None = None   # only honoured with backend.kind = "mock"
    mesh: MeshConfig = field(default_factory=MeshConfig)
    limits: Limits = field(default_factory=Limits)
    location: LocationConfig = field(default_factory=LocationConfig)
    visibility: VisibilityConfig = field(default_factory=VisibilityConfig)
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)

    @property
    def key_path(self) -> Path:
        return Path(self.data_dir).expanduser() / self.name / "node_key.pem"

    def validate(self) -> "NodeConfig":
        validate_mesh_bind(self.mesh.bind_host, self.mesh.allow_non_loopback)
        if self.mesh.internet and self.mesh.listen:
            raise ValueError("mesh.internet requires mesh.listen = false (internet nodes open no ports)")
        self.limits.validate()
        if self.location.precision not in PRECISIONS:
            raise ValueError(f"location.precision must be one of {sorted(PRECISIONS)}")
        if self.backend.kind not in ("managed", "none", "mock", "openai"):
            raise ValueError("backend.kind must be managed, openai, mock or none")
        if self.swarm.engine not in ("auto", "torch", "gguf", "mock"):
            raise ValueError("swarm.engine must be auto, torch, gguf or mock")
        if self.simulated_hardware is not None and self.backend.kind != "mock" and not self.backend.server_cmd:
            raise ValueError("simulated_hardware is only allowed with mock or test model servers")
        return self


def _build(cls, data: dict):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load(path: str | Path) -> NodeConfig:
    raw = tomllib.loads(Path(path).read_text())
    sub = {
        "mesh": MeshConfig, "limits": Limits, "location": LocationConfig, "backend": BackendConfig,
        "visibility": VisibilityConfig, "swarm": SwarmConfig,
    }
    kwargs = {k: v for k, v in raw.items() if k not in sub}
    for key, cls in sub.items():
        kwargs[key] = _build(cls, raw.get(key, {}))
    return _build(NodeConfig, kwargs).validate()

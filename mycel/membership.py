"""Mesh membership via signed push-pull gossip, restricted to certified members.

Each node periodically signs an *advert* describing itself (capabilities, models, current
budget, optional coarse location, and its membership proof) with a
monotonically increasing sequence number. Any member can relay any advert but cannot forge
or alter one, because adverts are signed by their origin. Adverts without a valid proof for
*this* mesh (a global ticket, or a private certificate chain) are dropped.

A node is "alive" while its newest advert is younger than ``advert_ttl_s``.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field

from .identity import EnvelopeError, verify_relayed
from .location import sanitize_remote_cell
from .trust import TrustError, TrustState

MAX_ADVERTS_PER_GOSSIP = 256     # hard cap accepted from a peer
GOSSIP_SAMPLE = 24               # adverts we send per exchange: own + a random sample (epidemic spread)
MAX_PEERS = 2000                 # bounded view: memory and CPU stay flat as the mesh grows
MAX_MODELS = 32


@dataclass
class PeerRecord:
    node_id: str
    advert: dict            # the signed envelope, relayed verbatim
    received_at: float      # local monotonic time
    # Locally observed reputation (never gossiped — each node forms its own view).
    ok: int = 0
    failed: int = 0
    observed_tps: float = 0.0
    rtt_ms: float = 0.0

    @property
    def body(self) -> dict:
        return self.advert["body"]

    @property
    def seq(self) -> int:
        return self.advert["seq"]

    @property
    def reliability(self) -> float:
        n = self.ok + self.failed
        return 1.0 if n == 0 else (self.ok + 1) / (n + 2)  # Laplace-smoothed


@dataclass
class Membership:
    self_id: str
    ttl_s: float
    trust: TrustState
    max_peers: int = MAX_PEERS
    peers: dict[str, PeerRecord] = field(default_factory=dict)
    rejected: int = 0
    _verified: dict[str, tuple] = field(default_factory=dict)  # node -> (proof hash, expiry) already checked

    def _sanitize(self, body: dict) -> dict | None:
        addr, relays = body.get("addr"), body.get("relays")
        if addr is not None and (not isinstance(addr, str) or ":" not in addr or len(addr) > 128):
            return None
        if relays is not None and (not isinstance(relays, list) or len(relays) > 4
                                   or not all(isinstance(u, str) and len(u) <= 200 for u in relays)):
            return None
        if addr is None and not relays:
            return None  # unreachable
        if not isinstance(body.get("kx", ""), str) or len(body.get("kx", "")) > 64:
            return None
        stages = body.get("stages", [])
        if not isinstance(stages, list) or len(stages) > 4 or not all(
            isinstance(s, dict) and isinstance(s.get("m"), str) and len(s["m"]) <= 128
            and isinstance(s.get("s"), int) and isinstance(s.get("e"), int) and 0 <= s["s"] < s["e"] <= 1024
            for s in stages
        ):
            return None
        models = body.get("models", [])
        if not isinstance(models, list) or len(models) > MAX_MODELS or not all(
            isinstance(m, str) and len(m) <= 128 for m in models
        ):
            return None
        loc = body.get("location")
        if loc is not None and sanitize_remote_cell(loc) != loc:
            return None  # finer-than-allowed or malformed location: reject, never rewrite a signed advert
        return body

    def _is_member(self, env: dict) -> bool:
        proof = env["body"].get("membership")
        node = env["from"]
        try:
            key = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
        except (TypeError, ValueError):
            return False
        cached = self._verified.get(node)
        if cached and cached[0] == key and time.time() < cached[1] and node not in self.trust.blocked \
                and not self.trust.proof_subjects(proof) & self.trust.revoked:
            return True
        try:
            self.trust.check_member(proof, node, env["pubkey"])
        except TrustError:
            return False
        expires = float("inf")
        if proof.get("kind") == "private":
            expires = min(c["body"]["exp"] for c in proof["chain"])
        self._verified[node] = (key, expires)
        return True

    def ingest(self, env: dict, now: float | None = None) -> bool:
        """Verify and store one advert. Returns True if it was new information."""
        now = time.monotonic() if now is None else now
        # Cheap duplicate check first: most gossip is news we already have, and signature checks
        # dominate CPU at scale. (A forged higher seq still fails verification below.)
        known = self.peers.get(env.get("from")) if isinstance(env.get("from"), str) else None
        if known is not None and isinstance(env.get("seq"), int) and env["seq"] <= known.seq:
            return False
        try:
            verify_relayed(env, expected_type="advert", max_age_s=self.ttl_s)
        except EnvelopeError:
            self.rejected += 1
            return False
        node = env["from"]
        if node == self.self_id:
            return False
        current = self.peers.get(node)
        if current is not None and env["seq"] <= current.seq:
            return False
        if self._sanitize(env["body"]) is None or not self._is_member(env):
            self.rejected += 1
            return False
        if current is None:
            if len(self.peers) >= self.max_peers and not self._evict(env, now):
                return False
            self.peers[node] = PeerRecord(node, env, now)
        else:
            current.advert, current.received_at = env, now
        return True

    def apply_revocations(self) -> None:
        """Drop peers that are blocked locally or whose membership depends on a revoked member."""
        for node, rec in list(self.peers.items()):
            subjects = self.trust.proof_subjects(rec.body.get("membership")) | {node}
            if subjects & (self.trust.revoked | self.trust.blocked):
                del self.peers[node]
                self._verified.pop(node, None)

    def _evict(self, incoming: dict, now: float) -> bool:
        """Make room in a full view. Prefer dropping dead, then idle clients, then the stalest peer —
        but never drop a peer we've worked with successfully for an unknown newcomer."""
        dead = [k for k, p in self.peers.items() if now - p.received_at >= self.ttl_s]
        if dead:
            del self.peers[dead[0]]
            return True
        clients = [p for p in self.peers.values() if p.body.get("role") != "server" and p.ok == 0]
        if clients and incoming["body"].get("role") == "server":
            del self.peers[min(clients, key=lambda p: p.received_at).node_id]
            return True
        candidates = [p for p in self.peers.values() if p.ok == 0]
        if not candidates:
            return False
        del self.peers[min(candidates, key=lambda p: p.received_at).node_id]
        return True

    def alive(self, now: float | None = None) -> list[PeerRecord]:
        now = time.monotonic() if now is None else now
        return [p for p in self.peers.values() if now - p.received_at < self.ttl_s]

    def prune(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        dead = [k for k, p in self.peers.items() if now - p.received_at >= self.ttl_s * 4]
        for k in dead:
            del self.peers[k]

    def adverts_for_gossip(self, own: dict, sample: int = GOSSIP_SAMPLE) -> list[dict]:
        """Own advert plus a random sample biased toward fresh news: half newest, half uniform.
        Constant-size messages; information still reaches all N nodes in O(log N) rounds."""
        alive = self.alive()
        if len(alive) <= sample - 1:
            return [own] + [p.advert for p in alive]
        half = (sample - 1) // 2
        newest = sorted(alive, key=lambda p: p.received_at, reverse=True)[:half]
        newest_ids = {p.node_id for p in newest}
        rest = [p for p in alive if p.node_id not in newest_ids]
        return [own] + [p.advert for p in newest] + [p.advert for p in random.sample(rest, sample - 1 - half)]

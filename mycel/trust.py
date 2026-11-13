"""Who may take part in a mesh.

**The global mesh** (the default everyone joins) has no founder and no authority. A node takes
part by presenting an *identity ticket*: a proof-of-work over its own public key
(``sha256(GLOBAL_TAG || pubkey || nonce)`` with ``GLOBAL_BITS`` leading zero bits). Minting one
costs a device about a second of CPU once; minting thousands of fake identities costs
thousands of times that. That raises the price of a Sybil flood without anyone in charge.
It does not make the mesh exclusive: anything that speaks the protocol and pays the work can
join. Abuse is handled locally (per-peer rate limits, measured reliability, block lists).

**Private meshes** (advanced) are invitation-only. A mesh is identified by its founder's key,
and members hold a chain of signed certificates back to the founder. Each certificate is bound
to one device key, expires, and may grant invite rights. The founder can revoke members, and
the founder-signed revocation list spreads by gossip. Joining is a two-paste pairing:
  1. The newcomer shows a **join code** (``myj1_…``: node id + public key).
  2. A member with invite rights returns a **membership code** (``mym1_…``: chain + bootstrap
     peers), which only works with the newcomer's private key.
"""
from __future__ import annotations

import base64
import json
import time
import zlib
from dataclasses import dataclass, field

import hashlib

from .identity import EnvelopeError, Identity, b64d, node_id_for, verify_static

GLOBAL_MESH_ID = "global"
GLOBAL_TAG = b"mycel-global-v1"
GLOBAL_BITS = 22          # minimum accepted work; tests lower this
MAX_DEPTH = 6
DEFAULT_TTL_S = 365 * 86400
JOIN_PREFIX, MEMBER_PREFIX = "myj1_", "mym1_"


class TrustError(Exception):
    pass


def _cert_body(env: dict) -> dict:
    return env["body"]


def issue_root(founder: Identity, ttl_s: float = 10 * DEFAULT_TTL_S) -> dict:
    now = time.time()
    return founder.seal("member-cert", {
        "mesh": founder.node_id, "sub": founder.node_id, "sub_pubkey": founder.pubkey,
        "iss": founder.node_id, "can_invite": True, "depth": 0, "iat": now, "exp": now + ttl_s,
    })


def issue_cert(issuer: Identity, issuer_chain: list[dict], sub_node_id: str, sub_pubkey: str,
               can_invite: bool = False, ttl_s: float = DEFAULT_TTL_S) -> list[dict]:
    """Issue a certificate for another node; returns the subject's full chain."""
    if not issuer_chain:
        raise TrustError("issuer is not a member")
    leaf = _cert_body(issuer_chain[-1])
    if leaf["sub"] != issuer.node_id:
        raise TrustError("issuer chain does not belong to issuer")
    if not leaf["can_invite"]:
        raise TrustError("your membership does not include invite rights")
    depth = leaf["depth"] + 1
    if depth > MAX_DEPTH:
        raise TrustError("invitation chain too deep")
    if node_id_for(b64d(sub_pubkey)) != sub_node_id:
        raise TrustError("join code is corrupted (node id does not match key)")
    now = time.time()
    exp = min(now + ttl_s, min(_cert_body(c)["exp"] for c in issuer_chain))
    cert = issuer.seal("member-cert", {
        "mesh": leaf["mesh"], "sub": sub_node_id, "sub_pubkey": sub_pubkey, "iss": issuer.node_id,
        "can_invite": bool(can_invite), "depth": depth, "iat": now, "exp": exp,
    })
    return [*issuer_chain, cert]


def verify_chain(chain, mesh_id: str, subject: str, subject_pubkey: str,
                 revoked: frozenset[str] = frozenset(), now: float | None = None) -> None:
    """Raise TrustError unless ``chain`` proves ``subject`` is a member of ``mesh_id``."""
    now = time.time() if now is None else now
    if not isinstance(chain, list) or not 1 <= len(chain) <= MAX_DEPTH + 1:
        raise TrustError("missing or oversized certificate chain")
    prev = None
    for i, env in enumerate(chain):
        try:
            verify_static(env, expected_type="member-cert")
            b = _cert_body(env)
            fields = (b["mesh"], b["sub"], b["sub_pubkey"], b["iss"], b["can_invite"], b["depth"], b["exp"])
        except (EnvelopeError, KeyError, TypeError) as exc:
            raise TrustError(f"certificate {i} invalid") from exc
        if b["mesh"] != mesh_id:
            raise TrustError("certificate is for a different mesh")
        if b["depth"] != i or b["exp"] < now:
            raise TrustError(f"certificate {i} expired or out of order")
        if node_id_for(b64d(b["sub_pubkey"])) != b["sub"]:
            raise TrustError(f"certificate {i} subject key mismatch")
        if b["sub"] in revoked:
            raise TrustError("membership revoked")
        if i == 0:
            if not (b["sub"] == b["iss"] == mesh_id == env["from"]):
                raise TrustError("chain does not start at the mesh founder")
        else:
            if not prev["can_invite"]:
                raise TrustError(f"certificate {i} issued by a member without invite rights")
            if b["iss"] != prev["sub"] or env["from"] != prev["sub"] or env["pubkey"] != prev["sub_pubkey"]:
                raise TrustError(f"certificate {i} not issued by the previous member")
        prev = b
    if prev["sub"] != subject or prev["sub_pubkey"] != subject_pubkey:
        raise TrustError("certificate chain belongs to another node")


# ---------------------------------------------------------------- global mesh tickets
def _work(pubkey_raw: bytes, nonce: int) -> int:
    digest = hashlib.sha256(GLOBAL_TAG + pubkey_raw + nonce.to_bytes(8, "big")).digest()
    return 256 - int.from_bytes(digest, "big").bit_length()  # leading zero bits


def mint_ticket(identity: Identity, bits: int | None = None) -> dict:
    """Find a nonce proving `bits` of work over this node's key. ~2**bits hashes."""
    return mint_ticket_for(identity.pubkey, GLOBAL_BITS if bits is None else bits)


def mint_ticket_for(pubkey: str, bits: int) -> dict:
    """Picklable form of mint_ticket (works in a process pool)."""
    raw = b64d(pubkey)
    pre = hashlib.sha256(GLOBAL_TAG + raw)
    target_bits = 256 - bits
    nonce = 0
    while True:
        h = pre.copy()
        h.update(nonce.to_bytes(8, "big"))
        if int.from_bytes(h.digest(), "big").bit_length() <= target_bits:
            return {"mesh": GLOBAL_MESH_ID, "pubkey": pubkey, "nonce": nonce, "bits": bits}
        nonce += 1


def verify_ticket(ticket, node_id: str, pubkey: str) -> None:
    if not isinstance(ticket, dict) or ticket.get("mesh") != GLOBAL_MESH_ID:
        raise TrustError("missing global mesh ticket")
    if ticket.get("pubkey") != pubkey or not isinstance(ticket.get("nonce"), int) or not 0 <= ticket["nonce"] < 2**64:
        raise TrustError("ticket belongs to another node")
    try:
        raw = b64d(pubkey)
    except ValueError as exc:
        raise TrustError("bad key") from exc
    if node_id_for(raw) != node_id:
        raise TrustError("ticket belongs to another node")
    if _work(raw, ticket["nonce"]) < GLOBAL_BITS:
        raise TrustError("not enough proof of work")


# ---------------------------------------------------------------- shareable codes
def _pack(prefix: str, obj: dict) -> str:
    raw = zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 9)
    return prefix + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unpack(prefix: str, code: str) -> dict:
    code = "".join(code.split())
    if not code.startswith(prefix):
        raise TrustError("that doesn't look like the right kind of code")
    try:
        body = code[len(prefix):]
        d = zlib.decompressobj()
        raw = d.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), 64_000)
        if d.unconsumed_tail:  # refuse decompression bombs
            raise ValueError
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError
        return obj
    except (ValueError, zlib.error) as exc:
        raise TrustError("code is incomplete or corrupted") from exc


def join_code(identity: Identity) -> str:
    return _pack(JOIN_PREFIX, {"node_id": identity.node_id, "pubkey": identity.pubkey})


def parse_join_code(code: str) -> tuple[str, str]:
    obj = _unpack(JOIN_PREFIX, code)
    node_id, pubkey = obj.get("node_id"), obj.get("pubkey")
    if not isinstance(node_id, str) or not isinstance(pubkey, str):
        raise TrustError("join code is missing fields")
    try:
        if node_id_for(b64d(pubkey)) != node_id:
            raise TrustError("join code is corrupted")
    except ValueError as exc:
        raise TrustError("join code is corrupted") from exc
    return node_id, pubkey


def membership_code(chain: list[dict], bootstrap: list[str]) -> str:
    return _pack(MEMBER_PREFIX, {"mesh": _cert_body(chain[0])["mesh"], "chain": chain, "bootstrap": bootstrap[:8]})


def parse_membership_code(code: str, me: Identity) -> tuple[str, list[dict], list[str]]:
    obj = _unpack(MEMBER_PREFIX, code)
    mesh_id, chain, bootstrap = obj.get("mesh"), obj.get("chain"), obj.get("bootstrap", [])
    if not isinstance(mesh_id, str) or not isinstance(bootstrap, list):
        raise TrustError("membership code is missing fields")
    verify_chain(chain, mesh_id, me.node_id, me.pubkey)
    return mesh_id, chain, [b for b in bootstrap if isinstance(b, str)][:8]


# ---------------------------------------------------------------- per-node trust state
@dataclass
class TrustState:
    mesh_id: str | None = None          # GLOBAL_MESH_ID or a private founder's node id
    chain: list[dict] = field(default_factory=list)   # private meshes
    ticket: dict | None = None          # global mesh
    revocation: dict | None = None      # private: founder-signed {"mesh", "revoked": [...]} with seq
    blocked: set[str] = field(default_factory=set)    # local-only block list (any mesh)

    @property
    def kind(self) -> str | None:
        if self.mesh_id is None:
            return None
        return "global" if self.mesh_id == GLOBAL_MESH_ID else "private"

    @property
    def is_member(self) -> bool:
        return bool(self.ticket) if self.kind == "global" else bool(self.chain)

    @property
    def revoked(self) -> frozenset[str]:
        return frozenset(self.revocation["body"]["revoked"]) if self.revocation else frozenset()

    @property
    def can_invite(self) -> bool:
        return self.kind == "private" and bool(self.chain) and _cert_body(self.chain[-1])["can_invite"]

    @property
    def is_founder(self) -> bool:
        return self.kind == "private" and len(self.chain) == 1

    def proof(self) -> dict | None:
        """What this node attaches to its adverts to show it belongs."""
        if self.kind == "global":
            return {"kind": "global", "ticket": self.ticket}
        if self.kind == "private":
            return {"kind": "private", "chain": self.chain}
        return None

    def check_member(self, proof, node_id: str, pubkey: str) -> None:
        if not self.mesh_id:
            raise TrustError("this node has not joined a mesh")
        if node_id in self.blocked:
            raise TrustError("blocked on this device")
        if not isinstance(proof, dict) or proof.get("kind") != self.kind:
            raise TrustError("member of a different mesh")
        if self.kind == "global":
            verify_ticket(proof.get("ticket"), node_id, pubkey)
        else:
            verify_chain(proof.get("chain"), self.mesh_id, node_id, pubkey, self.revoked)

    def proof_subjects(self, proof) -> set[str]:
        """Every node a proof depends on (for revocation / block checks)."""
        if isinstance(proof, dict) and proof.get("kind") == "private":
            try:
                return {c["body"]["sub"] for c in proof.get("chain") or []}
            except (KeyError, TypeError):
                return set()
        return set()

    def accept_revocation(self, env) -> bool:
        """Adopt a newer founder-signed revocation list (private meshes only)."""
        if self.kind != "private" or not isinstance(env, dict):
            return False
        try:
            verify_static(env, expected_type="revocation")
        except EnvelopeError:
            return False
        b = env["body"]
        if env["from"] != self.mesh_id or b.get("mesh") != self.mesh_id or not isinstance(env.get("seq"), int):
            return False
        if not isinstance(b.get("revoked"), list) or len(b["revoked"]) > 100_000:
            return False
        if self.revocation and env["seq"] <= self.revocation["seq"]:
            return False
        self.revocation = env
        return True

    def revoke(self, founder: Identity, node_id: str) -> dict:
        if not self.is_founder or founder.node_id != self.mesh_id:
            raise TrustError("only the founder of a private mesh can remove members")
        if node_id == self.mesh_id:
            raise TrustError("the founder can't remove themselves")
        revoked = sorted(self.revoked | {node_id})
        seq = (self.revocation["seq"] + 1) if self.revocation else 1
        self.revocation = founder.seal("revocation", {"mesh": self.mesh_id, "revoked": revoked}, seq=seq)
        return self.revocation

    def to_json(self) -> dict:
        return {"mesh_id": self.mesh_id, "chain": self.chain, "ticket": self.ticket,
                "revocation": self.revocation, "blocked": sorted(self.blocked)}

    @classmethod
    def from_json(cls, data: dict) -> "TrustState":
        return cls(data.get("mesh_id"), data.get("chain") or [], data.get("ticket"), data.get("revocation"),
                   set(data.get("blocked") or []))

"""Node identity and signed message envelopes.

Every node is identified by an Ed25519 public key. The node id is a hash of that key,
so ids are self-certifying: a message claiming to come from node X must carry X's
public key and a valid signature, and the key must hash to X.

Two envelope flavours share one wire format:

* **direct** envelopes (requests between two peers) carry a ``to`` field, a timestamp and
  a nonce. Receivers enforce a freshness window and a replay cache, and reject envelopes
  addressed to some other node.
* **relayed** envelopes (gossiped node adverts) are re-forwarded by third parties, so they
  have no ``to`` and no replay cache; instead they carry a per-origin sequence number, and
  receivers keep only the highest sequence they have seen.

Primitives come from ``cryptography`` (Ed25519). The envelope protocol itself is
prototype code and needs independent security review before carrying real traffic.
Transport confidentiality is delegated to the network layer (see README: overlay).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PROTOCOL_VERSION = 1
MAX_CLOCK_SKEW_S = 30.0
DIRECT_FRESHNESS_S = 60.0


class EnvelopeError(Exception):
    """Raised when an envelope fails verification. Message is safe to return to peers."""


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def node_id_for(pubkey_raw: bytes) -> str:
    digest = hashlib.sha256(b"mycel-node-v1" + pubkey_raw).digest()
    return base64.b32encode(digest).decode().lower().rstrip("=")[:26]


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class Identity:
    private_key: Ed25519PrivateKey

    @property
    def pubkey_raw(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def pubkey(self) -> str:
        return b64e(self.pubkey_raw)

    @property
    def node_id(self) -> str:
        return node_id_for(self.pubkey_raw)

    @classmethod
    def generate(cls) -> "Identity":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        path = Path(path).expanduser()
        if path.exists():
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError(f"{path} is readable by other users; run: chmod 600 {path}")
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 key")
            return cls(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ident = cls.generate()
        pem = ident.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return ident

    def seal(self, msg_type: str, body: dict, *, to: str | None = None, seq: int | None = None) -> dict:
        env = {
            "v": PROTOCOL_VERSION,
            "type": msg_type,
            "from": self.node_id,
            "pubkey": self.pubkey,
            "ts": time.time(),
            "body": body,
        }
        if to is not None:
            env["to"] = to
            env["nonce"] = secrets.token_hex(16)
        if seq is not None:
            env["seq"] = seq
        env["sig"] = b64e(self.private_key.sign(canonical(env)))
        return env


def _verify_signature(env: dict) -> None:
    try:
        sig = b64d(env["sig"])
        pub_raw = b64d(env["pubkey"])
        unsigned = {k: v for k, v in env.items() if k != "sig"}
        if node_id_for(pub_raw) != env["from"]:
            raise EnvelopeError("node id does not match public key")
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, canonical(unsigned))
    except EnvelopeError:
        raise
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise EnvelopeError("bad signature or malformed envelope") from exc
    if env.get("v") != PROTOCOL_VERSION:
        raise EnvelopeError("unsupported protocol version")
    if not isinstance(env.get("ts"), (int, float)) or not isinstance(env.get("body"), dict):
        raise EnvelopeError("malformed envelope")


class ReplayCache:
    """Remembers nonces for the freshness window so a captured request can't be replayed."""

    def __init__(self, window_s: float = DIRECT_FRESHNESS_S, max_entries: int = 100_000):
        self.window_s = window_s
        self.max_entries = max_entries
        self._seen: dict[str, float] = {}

    def check_and_add(self, key: str, now: float) -> None:
        if len(self._seen) > self.max_entries:
            cutoff = now - self.window_s - MAX_CLOCK_SKEW_S
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
            if len(self._seen) > self.max_entries:
                raise EnvelopeError("replay cache full; try later")
        if key in self._seen:
            raise EnvelopeError("replayed envelope")
        self._seen[key] = now


def verify_direct(env: dict, *, expected_type: str, me: str, replay: ReplayCache, now: float | None = None) -> dict:
    """Verify a peer-to-peer envelope addressed to ``me``. Returns the envelope."""
    now = time.time() if now is None else now
    _verify_signature(env)
    if env.get("type") != expected_type:
        raise EnvelopeError("unexpected message type")
    if env.get("to") != me:
        raise EnvelopeError("envelope addressed to another node")
    age = now - env["ts"]
    if age > DIRECT_FRESHNESS_S or age < -MAX_CLOCK_SKEW_S:
        raise EnvelopeError("stale or future-dated envelope (check clock sync)")
    nonce = env.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 32:
        raise EnvelopeError("missing nonce")
    replay.check_and_add(f"{env['from']}:{nonce}", now)
    return env


def verify_relayed(env: dict, *, expected_type: str, max_age_s: float, now: float | None = None) -> dict:
    """Verify a gossiped envelope that may arrive via third parties. Returns the envelope."""
    now = time.time() if now is None else now
    _verify_signature(env)
    if env.get("type") != expected_type:
        raise EnvelopeError("unexpected message type")
    if not isinstance(env.get("seq"), int):
        raise EnvelopeError("missing sequence number")
    age = now - env["ts"]
    if age > max_age_s or age < -MAX_CLOCK_SKEW_S:
        raise EnvelopeError("expired or future-dated advert")
    return env


def verify_static(env: dict, *, expected_type: str) -> dict:
    """Verify a long-lived signed document (e.g. a membership certificate). Returns it."""
    if not isinstance(env, dict):
        raise EnvelopeError("malformed document")
    _verify_signature(env)
    if env.get("type") != expected_type:
        raise EnvelopeError("unexpected document type")
    return env

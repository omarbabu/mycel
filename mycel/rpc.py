"""End-to-end encryption for every node-to-node message.

Each node has two keys: its Ed25519 identity (signatures; its hash is the node id) and an
X25519 key-agreement key whose public half rides in the node's *signed* advert, so the
binding "node id -> encryption key" is as strong as the advert signature.

A request is the sender's normal signed envelope, sealed to the recipient with **HPKE**
(RFC 9180, base mode, X25519 + HKDF-SHA256 + ChaCha20-Poly1305) from ``cryptography``. No
custom primitives. The sealed plaintext also carries a fresh 256-bit response key; every
response chunk is ChaCha20-Poly1305 under that key with a counter nonce, so only the two
endpoints can read the stream, and a relay can neither read, reorder, drop-undetected nor
splice chunks between streams.

What a relay (or anyone on the path) still learns: who talks to whom (node ids), when, and
message sizes. The envelope inside the encryption is signed and addressed to the recipient,
with a nonce and timestamp, so the existing replay and freshness checks apply unchanged.
"""
from __future__ import annotations

import json
import os
import stat
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .identity import b64d, b64e, canonical

SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
INFO = b"mycel-rpc-v1|"
MAX_CHUNK = 4 << 20


class SealError(Exception):
    """Decryption or framing failed. Safe to report to peers."""


class KxKey:
    """This node's X25519 key-agreement key (private part never leaves the data folder)."""

    def __init__(self, key: X25519PrivateKey):
        self.key = key
        self.public_b64 = b64e(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    @classmethod
    def generate(cls) -> "KxKey":
        return cls(X25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "KxKey":
        path = Path(path).expanduser()
        if path.exists():
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError(f"{path} is readable by other users; run: chmod 600 {path}")
            return cls(X25519PrivateKey.from_private_bytes(path.read_bytes()))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        k = X25519PrivateKey.generate()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
        return cls(k)


def _public(kx_b64: str) -> X25519PublicKey:
    try:
        raw = b64d(kx_b64)
        if len(raw) != 32:
            raise ValueError
        return X25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise SealError("bad encryption key") from exc


def seal_request(peer_kx_b64: str, to_id: str, method: str, env: dict) -> tuple[bytes, bytes]:
    """Encrypt a signed envelope to a peer. Returns (ciphertext, response key)."""
    resp_key = os.urandom(32)
    plaintext = canonical({"m": method, "env": env, "rk": b64e(resp_key)})
    return SUITE.encrypt(plaintext, _public(peer_kx_b64), INFO + to_id.encode()), resp_key


def open_request(kx: KxKey, my_id: str, blob: bytes) -> tuple[str, dict, bytes]:
    """Decrypt a request addressed to this node. Returns (method, signed envelope, response key)."""
    try:
        data = json.loads(SUITE.decrypt(blob, kx.key, INFO + my_id.encode()))
        method, env, rk = data["m"], data["env"], b64d(data["rk"])
        if not isinstance(method, str) or not isinstance(env, dict) or len(rk) != 32:
            raise ValueError
    except (InvalidTag, ValueError, KeyError, TypeError) as exc:
        raise SealError("could not decrypt request") from exc
    return method, env, rk


class StreamSealer:
    """Encrypts response items in order. One per response stream."""

    def __init__(self, key: bytes):
        self._aead, self._n = ChaCha20Poly1305(key), 0

    def seal(self, item: dict) -> bytes:
        nonce = struct.pack(">IQ", 0, self._n)
        self._n += 1
        return self._aead.encrypt(nonce, json.dumps(item, separators=(",", ":")).encode(), b"mycel-resp-v1")


class StreamOpener:
    """Decrypts response items; any reordering, replay or tampering fails authentication."""

    def __init__(self, key: bytes):
        self._aead, self._n = ChaCha20Poly1305(key), 0

    def open(self, chunk: bytes) -> dict:
        nonce = struct.pack(">IQ", 0, self._n)
        try:
            item = json.loads(self._aead.decrypt(nonce, chunk, b"mycel-resp-v1"))
        except (InvalidTag, ValueError) as exc:
            raise SealError("response chunk failed authentication") from exc
        self._n += 1
        if not isinstance(item, dict):
            raise SealError("malformed response item")
        return item

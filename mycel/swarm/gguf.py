"""Read (and write) GGUF model files — the format llama.cpp and every node already use.

A swarm stage needs only *its* layers. ``GGUF`` memory-maps a file and dequantizes just the
tensors asked for, and ``fetch_slice`` downloads only the byte ranges of a layer slice (plus
the header) with HTTP Range requests, so an 8 GB node hosting 14 of a 70B model's 80 layers
fetches ~7 GB instead of 42 GB.

Supported tensor types: F32, F16, BF16, Q8_0, Q4_0, Q4_1, Q5_0, Q5_1, Q4_K, Q5_K, Q6_K
(everything in the Q4_K_M / Q8_0 files Mycel's catalog uses).
"""
from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

GGUF_MAGIC = b"GGUF"
F32, F16, Q4_0, Q4_1, Q5_0, Q5_1, Q8_0 = 0, 1, 2, 3, 6, 7, 8
Q4_K, Q5_K, Q6_K, BF16 = 12, 13, 14, 30
BLOCK = {F32: (1, 4), F16: (1, 2), BF16: (1, 2), Q4_0: (32, 18), Q4_1: (32, 20), Q5_0: (32, 22),
         Q5_1: (32, 24), Q8_0: (32, 34), Q4_K: (256, 144), Q5_K: (256, 176), Q6_K: (256, 210)}
TYPE_NAMES = {F32: "F32", F16: "F16", BF16: "BF16", Q4_0: "Q4_0", Q4_1: "Q4_1", Q5_0: "Q5_0", Q5_1: "Q5_1",
              Q8_0: "Q8_0", Q4_K: "Q4_K", Q5_K: "Q5_K", Q6_K: "Q6_K"}
# KV value types
_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STR, _ARR, _U64, _I64, _F64 = range(13)
_SCALAR = {_U8: "<B", _I8: "<b", _U16: "<H", _I16: "<h", _U32: "<I", _I32: "<i", _F32: "<f", _BOOL: "<?",
           _U64: "<Q", _I64: "<q", _F64: "<d"}
_NP = {_U8: np.uint8, _I8: np.int8, _U16: np.uint16, _I16: np.int16, _U32: np.uint32, _I32: np.int32,
       _F32: np.float32, _BOOL: np.bool_, _U64: np.uint64, _I64: np.int64, _F64: np.float64}
LAYER_RE = re.compile(r"^blk\.(\d+)\.")


class GGUFError(Exception):
    pass


class NeedMore(Exception):
    """Header parsing ran past the bytes available (range downloads fetch more and retry)."""


@dataclass
class TensorInfo:
    name: str
    ne: tuple[int, ...]      # ggml order: ne[0] is the row length
    type: int
    offset: int              # absolute file offset of the data
    nbytes: int

    @property
    def shape(self) -> tuple[int, ...]:   # numpy order
        return tuple(reversed(self.ne))

    @property
    def layer(self) -> int | None:
        m = LAYER_RE.match(self.name)
        return int(m.group(1)) if m else None


class _Reader:
    def __init__(self, buf, pos: int = 0):
        self.buf, self.pos = buf, pos

    def take(self, n: int):
        if self.pos + n > len(self.buf):
            raise NeedMore(self.pos + n)
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        return bytes(self.take(self.unpack("<Q"))).decode("utf-8", errors="replace")

    def value(self, vtype: int):
        if vtype == _STR:
            return self.string()
        if vtype == _ARR:
            etype, n = self.unpack("<I"), self.unpack("<Q")
            if etype == _STR:
                return [self.string() for _ in range(n)]
            if etype in _NP:
                size = np.dtype(_NP[etype]).itemsize * n
                return np.frombuffer(bytes(self.take(size)), dtype=_NP[etype]).tolist()
            return [self.value(etype) for _ in range(n)]
        if vtype not in _SCALAR:
            raise GGUFError(f"unknown metadata type {vtype}")
        return self.unpack(_SCALAR[vtype])


def parse_header(buf) -> tuple[dict, list[TensorInfo], int]:
    """Parse metadata + tensor directory. Returns (kv, tensors, data_offset). Raises NeedMore."""
    r = _Reader(buf)
    if bytes(r.take(4)) != GGUF_MAGIC:
        raise GGUFError("not a GGUF file")
    version = r.unpack("<I")
    if version not in (2, 3):
        raise GGUFError(f"unsupported GGUF version {version}")
    n_tensors, n_kv = r.unpack("<Q"), r.unpack("<Q")
    kv = {}
    for _ in range(n_kv):
        key = r.string()
        kv[key] = r.value(r.unpack("<I"))
    infos = []
    for _ in range(n_tensors):
        name = r.string()
        nd = r.unpack("<I")
        ne = tuple(r.unpack("<Q") for _ in range(nd))
        ttype, off = r.unpack("<I"), r.unpack("<Q")
        infos.append((name, ne, ttype, off))
    align = int(kv.get("general.alignment", 32))
    data_off = (r.pos + align - 1) // align * align
    tensors = []
    for name, ne, ttype, off in infos:
        if ttype not in BLOCK:
            raise GGUFError(f"tensor {name} uses unsupported type {ttype}")
        n = int(np.prod(ne))
        be, bb = BLOCK[ttype]
        tensors.append(TensorInfo(name, ne, ttype, data_off + off, n // be * bb))
    return kv, tensors, data_off


# ---------------------------------------------------------------------------- dequantization
def _f16(raw) -> np.ndarray:
    return raw.view(np.float16).astype(np.float32)


def dequantize(raw: np.ndarray, ttype: int, n: int) -> np.ndarray:
    """Raw tensor bytes (uint8 array) -> float32 vector of n elements."""
    if ttype == F32:
        return raw.view(np.float32)[:n].copy()
    if ttype == F16:
        return raw.view(np.float16)[:n].astype(np.float32)
    if ttype == BF16:
        return (raw.view(np.uint16)[:n].astype(np.uint32) << 16).view(np.float32)
    be, bb = BLOCK[ttype]
    b = raw[: n // be * bb].reshape(-1, bb)
    if ttype == Q8_0:
        d = _f16(b[:, :2].copy())
        return (d * b[:, 2:].view(np.int8).astype(np.float32)).reshape(-1)
    if ttype in (Q4_0, Q4_1):
        d = _f16(b[:, :2].copy())
        qs = b[:, 4:] if ttype == Q4_1 else b[:, 2:]
        q = np.concatenate([qs & 0x0F, qs >> 4], axis=1).astype(np.float32)
        if ttype == Q4_0:
            return (d * (q - 8)).reshape(-1)
        return (d * q + _f16(b[:, 2:4].copy())).reshape(-1)
    if ttype in (Q5_0, Q5_1):
        d = _f16(b[:, :2].copy())
        o = 4 if ttype == Q5_1 else 2
        qh = b[:, o:o + 4].copy().view(np.uint32)[:, 0]
        qs = b[:, o + 4:]
        bits = ((qh[:, None] >> np.arange(32, dtype=np.uint32)[None, :]) & 1).astype(np.uint8) << 4
        q = (np.concatenate([qs & 0x0F, qs >> 4], axis=1) | bits).astype(np.float32)
        if ttype == Q5_0:
            return (d * (q - 16)).reshape(-1)
        return (d * q + _f16(b[:, 2:4].copy())).reshape(-1)
    if ttype in (Q4_K, Q5_K):
        d, dmin = _f16(b[:, 0:2].copy()), _f16(b[:, 2:4].copy())
        s = b[:, 4:16].astype(np.uint8)
        sc = np.empty((len(b), 8), np.float32)
        mn = np.empty((len(b), 8), np.float32)
        sc[:, :4], mn[:, :4] = s[:, 0:4] & 63, s[:, 4:8] & 63
        sc[:, 4:] = (s[:, 8:12] & 0x0F) | ((s[:, 0:4] >> 6) << 4)
        mn[:, 4:] = (s[:, 8:12] >> 4) | ((s[:, 4:8] >> 6) << 4)
        if ttype == Q4_K:
            qs = b[:, 16:144].reshape(-1, 4, 32)
            q = np.stack([qs & 0x0F, qs >> 4], axis=2).reshape(-1, 8, 32).astype(np.float32)
        else:
            qh = b[:, 16:48]
            qs = b[:, 48:176].reshape(-1, 4, 32)
            lo = np.stack([qs & 0x0F, qs >> 4], axis=2).reshape(-1, 8, 32)
            hb = ((qh[:, None, :] >> np.arange(8, dtype=np.uint8)[None, :, None]) & 1) << 4
            q = (lo | hb).astype(np.float32)
        return ((d * sc)[:, :, None] * q - (dmin * mn)[:, :, None]).reshape(-1)
    if ttype == Q6_K:
        ql, qh = b[:, 0:128], b[:, 128:192]
        scales = b[:, 192:208].view(np.int8).astype(np.float32)
        d = _f16(b[:, 208:210].copy())
        out = np.empty((len(b), 256), np.float32)
        for half in range(2):
            l = ql[:, 64 * half: 64 * half + 64]
            h = qh[:, 32 * half: 32 * half + 32]
            sc = scales[:, 8 * half: 8 * half + 8]
            q1 = ((l[:, :32] & 0x0F) | (((h >> 0) & 3) << 4)).astype(np.float32) - 32
            q2 = ((l[:, 32:] & 0x0F) | (((h >> 2) & 3) << 4)).astype(np.float32) - 32
            q3 = ((l[:, :32] >> 4) | (((h >> 4) & 3) << 4)).astype(np.float32) - 32
            q4 = ((l[:, 32:] >> 4) | (((h >> 6) & 3) << 4)).astype(np.float32) - 32
            base = 128 * half
            for j, qq in enumerate((q1, q2, q3, q4)):
                s2 = np.repeat(sc[:, 2 * j: 2 * j + 2], 16, axis=1)   # 16 values share a scale
                out[:, base + 32 * j: base + 32 * j + 32] = d * s2 * qq
        return out.reshape(-1)
    raise GGUFError(f"unsupported tensor type {ttype}")


def quantize_q8_0(x: np.ndarray) -> bytes:
    """float32 -> Q8_0 bytes (used to write small test models in the real on-disk format)."""
    x = x.astype(np.float32).reshape(-1, 32)
    amax = np.abs(x).max(axis=1, keepdims=True)
    d = (amax / 127.0).astype(np.float32)
    q = np.where(d > 0, np.round(x / np.where(d > 0, d, 1)), 0).astype(np.int8)
    out = np.empty((len(x), 34), np.uint8)
    out[:, :2] = d.astype(np.float16).view(np.uint8).reshape(-1, 2)
    out[:, 2:] = q.view(np.uint8)
    return out.tobytes()


# ---------------------------------------------------------------------------- reading
class GGUF:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._f = open(self.path, "rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            self.kv, tensors, self.data_offset = parse_header(memoryview(self._mm))
        except NeedMore as exc:
            raise GGUFError("truncated GGUF file") from exc
        self.tensors = {t.name: t for t in tensors}

    def close(self) -> None:
        try:
            self._mm.close()
        except BufferError:
            pass
        self._f.close()

    @property
    def arch(self) -> str:
        return str(self.kv.get("general.architecture", ""))

    def meta(self, key: str, default=None):
        return self.kv.get(f"{self.arch}.{key}", default)

    def has(self, name: str) -> bool:
        return name in self.tensors

    def raw(self, name: str) -> np.ndarray:
        t = self.tensors[name]
        return np.frombuffer(self._mm, dtype=np.uint8, count=t.nbytes, offset=t.offset)

    def array(self, name: str, dtype=np.float32) -> np.ndarray:
        t = self.tensors[name]
        n = int(np.prod(t.ne))
        return dequantize(self.raw(name), t.type, n).reshape(t.shape).astype(dtype, copy=False)

    def layer_count(self) -> int:
        return int(self.meta("block_count", 0))


# ---------------------------------------------------------------------------- writing
def _w_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def _w_value(v) -> bytes:
    if isinstance(v, bool):
        return struct.pack("<I?", _BOOL, v)
    if isinstance(v, int):
        return struct.pack("<Ii", _I32, v) if -2**31 <= v < 2**31 else struct.pack("<Iq", _I64, v)
    if isinstance(v, float):
        return struct.pack("<If", _F32, v)
    if isinstance(v, str):
        return struct.pack("<I", _STR) + _w_str(v)
    if isinstance(v, (list, tuple)):
        if all(isinstance(x, str) for x in v):
            return struct.pack("<IIQ", _ARR, _STR, len(v)) + b"".join(_w_str(x) for x in v)
        if all(isinstance(x, (int, np.integer)) and not isinstance(x, bool) for x in v):
            return struct.pack("<IIQ", _ARR, _I32, len(v)) + np.asarray(v, np.int32).tobytes()
        return struct.pack("<IIQ", _ARR, _F32, len(v)) + np.asarray(v, np.float32).tobytes()
    raise GGUFError(f"can't write metadata value {v!r}")


def write_gguf(path: str | Path, kv: dict, tensors: list[tuple[str, tuple[int, ...], int, bytes]], align: int = 32) -> None:
    """Write a GGUF v3 file. ``tensors`` = [(name, ne (ggml order), type, raw bytes)]."""
    head = GGUF_MAGIC + struct.pack("<IQQ", 3, len(tensors), len(kv) + ("general.alignment" not in kv))
    for k, v in kv.items():
        head += _w_str(k) + _w_value(v)
    if "general.alignment" not in kv:
        head += _w_str("general.alignment") + struct.pack("<II", _U32, align)
    off, dirs = 0, b""
    for name, ne, ttype, raw in tensors:
        dirs += _w_str(name) + struct.pack("<I", len(ne)) + b"".join(struct.pack("<Q", n) for n in ne)
        dirs += struct.pack("<IQ", ttype, off)
        off += (len(raw) + align - 1) // align * align
    body = head + dirs
    pad = (-len(body)) % align
    with open(path, "wb") as f:
        f.write(body + b"\0" * pad)
        for _, _, _, raw in tensors:
            f.write(raw)
            f.write(b"\0" * ((-len(raw)) % align))


def tensor_bytes(t: TensorInfo) -> int:
    return t.nbytes


def slice_names(tensors, start: int, end: int, bookends: bool = False) -> list[str]:
    """Tensors a stage hosting layers [start, end) needs (optionally plus embedding/head)."""
    out = []
    for t in tensors:
        layer = t.layer
        if layer is not None and start <= layer < end:
            out.append(t.name)
        elif layer is None and (bookends or t.name == "rope_freqs.weight"):
            out.append(t.name)
    return out


async def fetch_slice(session, url: str, dest: Path, start: int, end: int, bookends: bool = False,
                      progress=None) -> Path:
    """Download only the header and the tensors for layers [start, end) of a remote GGUF file
    (HTTP Range requests), and write them as a standalone GGUF slice at ``dest``."""
    want = 1 << 20
    while True:
        async with session.get(url, headers={"Range": f"bytes=0-{want - 1}"}) as r:
            if r.status not in (200, 206):
                raise GGUFError(f"HTTP {r.status} fetching GGUF header")
            buf = await r.read()
        try:
            kv, tensors, _ = parse_header(buf)
            break
        except NeedMore as more:
            if want > 256 << 20:
                raise GGUFError("GGUF header too large")
            want = max(want * 2, int(more.args[0]) + 4096)
    names = set(slice_names(tensors, start, end, bookends))
    picked = [t for t in tensors if t.name in names]
    total = sum(t.nbytes for t in picked) or 1
    done, out = 0, []
    for t in picked:
        async with session.get(url, headers={"Range": f"bytes={t.offset}-{t.offset + t.nbytes - 1}"}) as r:
            if r.status != 206:
                raise GGUFError("server does not support range requests")
            raw = await r.read()
        if len(raw) != t.nbytes:
            raise GGUFError(f"short read for {t.name}")
        out.append((t.name, t.ne, t.type, raw))
        done += t.nbytes
        if progress:
            progress(done / total)
    meta = dict(kv)
    meta["mycel.slice"] = f"{start}-{end}"
    meta.pop("general.alignment", None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    write_gguf(tmp, meta, out)
    tmp.rename(dest)
    return dest

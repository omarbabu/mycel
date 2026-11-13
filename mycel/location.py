"""Opt-in, coarse location sharing.

Location is OFF by default. When a user opts in, the node never transmits coordinates —
only a geohash *cell* at a coarse precision. Snapping to a fixed grid (rather than adding
random jitter) matters: re-randomised jitter can be averaged away by anyone watching
adverts over time, while a grid cell reveals exactly the same thing on every broadcast.

Coordinates are entered manually by the user. The node never calls a geo-IP service,
which would leak the node's address to a third party.
"""
from __future__ import annotations

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# precision name -> geohash length (approx. cell size)
PRECISIONS = {
    "continent": 1,  # ~5000 km
    "country": 2,    # ~1250 km
    "region": 3,     # ~156 km
    "metro": 4,      # ~39 km — finest allowed
}
FINEST = max(PRECISIONS.values())


def encode(lat: float, lon: float, length: int) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    if not 1 <= length <= FINEST:
        raise ValueError(f"geohash length must be 1..{FINEST}")
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, ch, even = [], 0, 0, True
    while len(out) < length:
        rng, val = (lon_rng, lon) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch = (ch << 1) | 1
            rng[0] = mid
        else:
            ch <<= 1
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)


def decode_center(cell: str) -> tuple[float, float, float, float]:
    """Return (lat, lon, lat_err, lon_err) for the centre of a geohash cell."""
    if not cell or len(cell) > FINEST or any(c not in _BASE32 for c in cell):
        raise ValueError("invalid geohash cell")
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for c in cell:
        idx = _BASE32.index(c)
        for shift in range(4, -1, -1):
            bit = (idx >> shift) & 1
            rng = lon_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if bit:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    lat = (lat_rng[0] + lat_rng[1]) / 2
    lon = (lon_rng[0] + lon_rng[1]) / 2
    return lat, lon, (lat_rng[1] - lat_rng[0]) / 2, (lon_rng[1] - lon_rng[0]) / 2


def public_cell(share: bool, lat: float | None, lon: float | None, precision: str) -> str | None:
    """The only location value a node ever puts on the wire."""
    if not share or lat is None or lon is None:
        return None
    if precision not in PRECISIONS:
        raise ValueError(f"precision must be one of {sorted(PRECISIONS)}")
    return encode(lat, lon, PRECISIONS[precision])


def sanitize_remote_cell(cell) -> str | None:
    """Validate a cell received from a peer; anything finer than allowed is truncated."""
    if not isinstance(cell, str) or not cell:
        return None
    cell = cell[:FINEST]
    try:
        decode_center(cell)
    except ValueError:
        return None
    return cell

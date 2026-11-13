"""Which relays a node uses to reach the internet mesh.

Three sources, in priority order:

* **yours** — relays you added (Settings → Network), e.g. one your community runs.
* **bundled** — the public seed relays shipped in ``seeds.json``. Only needed for the very
  first connection of a fresh install.
* **learned** — relays seen in other members' adverts that this node has connected to
  successfully. Persisted, so a node that joined once keeps finding the mesh even if every
  bundled relay goes away. That keeps the network founderless after day one.

A node keeps outbound connections to a few of them (``HOME_RELAYS``) and visits others on demand.
"""
from __future__ import annotations

import json
import random
import time
from importlib import resources
from pathlib import Path
from typing import Callable

from .store import _atomic_write

HOME_RELAYS = 3          # outbound connections a node keeps open
MAX_USER = 8
MAX_LEARNED = 32
LEARNED_TTL_S = 30 * 86400


def bundled_relays() -> list[str]:
    try:
        data = json.loads(resources.files("mycel").joinpath("seeds.json").read_text())
    except (FileNotFoundError, ValueError, OSError):
        return []
    urls = data.get("relays") if isinstance(data, dict) else None
    return [u for u in urls if isinstance(u, str)][:16] if isinstance(urls, list) else []


class RelayBook:
    def __init__(self, data_dir: Path, url_ok: Callable[[str], bool], configured: list[str] | None = None,
                 use_bundled: bool = True):
        self.path = data_dir / "relays.json"
        self.url_ok = url_ok
        self.configured = [u for u in dict.fromkeys(configured or [])]   # from config / --relay (not editable in the app)
        self.use_bundled = use_bundled
        self.user: list[str] = []
        self.learned: dict[str, float] = {}   # url -> last time we connected to it
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return
        self.user = [u for u in data.get("user", []) if isinstance(u, str) and self.url_ok(u)][:MAX_USER]
        if isinstance(data.get("use_bundled"), bool):
            self.use_bundled = data["use_bundled"]
        now = time.time()
        learned = data.get("learned", {})
        if isinstance(learned, dict):
            self.learned = {u: float(t) for u, t in learned.items()
                            if isinstance(u, str) and isinstance(t, (int, float)) and now - t < LEARNED_TTL_S
                            and self.url_ok(u)}

    def save(self) -> None:
        keep = sorted(self.learned.items(), key=lambda kv: -kv[1])[:MAX_LEARNED]
        _atomic_write(self.path, {"user": self.user, "use_bundled": self.use_bundled, "learned": dict(keep)})

    # ------------------------------------------------------------------ editing
    def add_user(self, url: str) -> None:
        url = url.strip()
        if not self.url_ok(url):
            raise ValueError("relay must be a wss:// URL on a public address (e.g. wss://relay.example.org/ws)")
        if url not in self.user:
            if len(self.user) >= MAX_USER:
                raise ValueError(f"at most {MAX_USER} relays of your own")
            self.user.append(url)
            self.save()

    def remove_user(self, url: str) -> None:
        if url in self.user:
            self.user.remove(url)
            self.save()

    def set_use_bundled(self, on: bool) -> None:
        self.use_bundled = bool(on)
        self.save()

    def note_connected(self, url: str) -> None:
        """Remember a relay we actually reached (only ones that passed the URL guard)."""
        if not self.url_ok(url):
            return
        fresh = url not in self.learned or time.time() - self.learned[url] > 3600
        self.learned[url] = time.time()
        if fresh:
            self.save()

    # ------------------------------------------------------------------ choosing
    def sources(self) -> dict[str, str]:
        """url -> where it came from (first source wins)."""
        out: dict[str, str] = {}
        for u in self.user:
            out.setdefault(u, "yours")
        for u in self.configured:
            out.setdefault(u, "config")
        if self.use_bundled:
            for u in bundled_relays():
                out.setdefault(u, "bundled")
        for u in self.learned:
            out.setdefault(u, "learned")
        return {u: s for u, s in out.items() if self.url_ok(u)}

    def home_set(self, n: int = HOME_RELAYS) -> list[str]:
        """The relays to stay connected to: all of yours, then bundled/config, then learned."""
        src = self.sources()
        mine = [u for u, s in src.items() if s in ("yours", "config")]
        seeds = [u for u, s in src.items() if s == "bundled"]
        learned = sorted((u for u, s in src.items() if s == "learned"), key=lambda u: -self.learned[u])
        random.shuffle(seeds)
        picked = mine[:]
        for u in seeds + learned:
            if len(picked) >= max(n, len(mine)):
                break
            picked.append(u)
        return picked

    def fallbacks(self) -> list[str]:
        """Everything known, for healing when every home relay is down."""
        return list(self.sources())

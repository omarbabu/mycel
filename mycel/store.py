"""Local persistence: conversations and a contribution ledger. Everything stays on this device."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

_ID = re.compile(r"^[a-f0-9]{16}$")
MAX_CONVERSATION_BYTES = 4 << 20


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class ConversationStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, cid: str) -> Path:
        if not isinstance(cid, str) or not _ID.match(cid):  # also blocks path traversal
            raise KeyError(cid)
        return self.root / f"{cid}.json"

    def list(self) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for p in self.root.glob("*.json"):
            try:
                c = json.loads(p.read_text())
            except ValueError:
                continue
            out.append({"id": c["id"], "title": c.get("title", "New chat"), "updated": c.get("updated", 0),
                        "model": c.get("model"), "pinned": bool(c.get("pinned")),
                        "preview": next((m.get("content", "")[:120] for m in reversed(c.get("messages", []))
                                         if m.get("role") in ("user", "assistant") and m.get("content")), "")})
        return sorted(out, key=lambda c: (not c["pinned"], -c["updated"]))

    def create(self, title: str = "New chat", model: str | None = None) -> dict:
        c = {"id": secrets.token_hex(8), "title": title, "model": model, "messages": [],
             "created": time.time(), "updated": time.time()}
        _atomic_write(self._path(c["id"]), c)
        return c

    def get(self, cid: str) -> dict:
        try:
            return json.loads(self._path(cid).read_text())
        except FileNotFoundError as exc:
            raise KeyError(cid) from exc

    def put(self, cid: str, data: dict) -> dict:
        current = self.get(cid)
        messages = data.get("messages", current["messages"])
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        updated = {**current, "messages": messages, "updated": time.time(),
                   "title": str(data.get("title", current["title"]))[:120],
                   "model": data.get("model", current.get("model")),
                   "pinned": bool(data.get("pinned", current.get("pinned", False)))}
        if len(json.dumps(updated)) > MAX_CONVERSATION_BYTES:
            raise ValueError("conversation is too large; start a new chat")
        _atomic_write(self._path(cid), updated)
        return updated

    def delete(self, cid: str) -> None:
        self._path(cid).unlink(missing_ok=True)


class Ledger:
    """Daily totals of what this node gave to and took from the mesh (for the 'impact' view)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {"days": {}, "first_seen": time.time()}
        self._dirty = 0

    def record(self, kind: str, tokens: int, peer: str | None = None) -> None:
        day = time.strftime("%Y-%m-%d")
        d = self.data["days"].setdefault(day, {"given": 0, "received": 0, "jobs_given": 0, "jobs_received": 0, "peers": []})
        d[kind] += tokens
        d["jobs_" + kind] += 1
        if peer and kind == "given" and peer not in d["peers"] and len(d["peers"]) < 500:
            d["peers"].append(peer)
        self._dirty += 1
        if self._dirty >= 5:
            self.flush()

    def flush(self) -> None:
        if self._dirty:
            days = dict(sorted(self.data["days"].items())[-120:])
            self.data["days"] = days
            _atomic_write(self.path, self.data)
            self._dirty = 0

    def summary(self, days: int = 14) -> dict:
        today = time.time()
        series = []
        for i in range(days - 1, -1, -1):
            key = time.strftime("%Y-%m-%d", time.localtime(today - i * 86400))
            d = self.data["days"].get(key, {})
            series.append({"day": key, "given": d.get("given", 0), "received": d.get("received", 0)})
        all_days = self.data["days"].values()
        peers = set()
        for d in all_days:
            peers.update(d.get("peers", []))
        t = self.data["days"].get(time.strftime("%Y-%m-%d"), {})
        today = {"given": t.get("given", 0), "received": t.get("received", 0),
                 "jobs_given": t.get("jobs_given", 0), "people": len(t.get("peers", []))}
        return {"series": series, "today": today,
                "total_given": sum(d.get("given", 0) for d in all_days),
                "total_received": sum(d.get("received", 0) for d in all_days),
                "people_helped": len(peers),
                "jobs_given": sum(d.get("jobs_given", 0) for d in all_days),
                "member_since": self.data.get("first_seen")}

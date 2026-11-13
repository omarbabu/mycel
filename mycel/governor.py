"""Hard limits on how much of this machine the mesh may use.

The owner sets:

* ``max_share``          fraction (0..1) of accelerator wall-clock time mesh jobs may use,
                         enforced as a token bucket of accelerator-seconds over ``window_s``.
* ``max_concurrent``     simultaneous mesh jobs.
* ``max_tokens_per_job`` output-token cap per mesh job.
* ``daily_seconds``      optional cap on accelerator-seconds per rolling 24 h (0 = no cap).
* ``per_peer_per_min``   jobs per minute any single requesting peer may start.
* ``paused``             kill switch.

"Hard" means: a job is only admitted while budget remains, the budget it may use is
reserved up front, and the job is cancelled at a deadline derived from that reservation. Charges are wall-clock and concurrent jobs are each charged in
full, so contention can only make the mesh use *less* than the limit, never more.

Jobs the owner runs on their own node are not mesh jobs and don't touch this budget.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field, asdict


@dataclass
class Limits:
    max_share: float = 0.25
    window_s: float = 60.0
    max_concurrent: int = 1
    max_tokens_per_job: int = 2048
    daily_seconds: float = 0.0
    per_peer_per_min: int = 6
    paused: bool = False

    def validate(self) -> "Limits":
        if not 0.0 <= self.max_share <= 1.0:
            raise ValueError("max_share must be between 0 and 1")
        if self.window_s < 10:
            raise ValueError("window_s must be >= 10")
        if self.max_concurrent < 0 or self.max_tokens_per_job < 1:
            raise ValueError("max_concurrent >= 0 and max_tokens_per_job >= 1 required")
        if self.daily_seconds < 0 or self.per_peer_per_min < 0:
            raise ValueError("daily_seconds and per_peer_per_min must be >= 0")
        return self


class Rejected(Exception):
    pass


@dataclass
class Grant:
    job_id: str
    peer: str
    max_tokens: int
    deadline: float
    started: float
    governor: "Governor" = field(repr=False)
    _done: bool = False

    def finish(self) -> float:
        if self._done:
            return 0.0
        self._done = True
        return self.governor._release(self)


class Governor:
    def __init__(self, limits: Limits, clock=time.monotonic):
        self.limits = limits.validate()
        self.clock = clock
        self._bucket = self.capacity
        self._last = clock()
        self._active: dict[str, Grant] = {}
        self._daily: deque[tuple[float, float]] = deque()
        self._peer_starts: dict[str, deque[float]] = {}
        self.stats = {"admitted": 0, "rejected": 0, "seconds_used": 0.0, "tokens_clamped": 0}

    @property
    def capacity(self) -> float:
        return self.limits.max_share * self.limits.window_s

    def update(self, **changes) -> Limits:
        new = Limits(**{**asdict(self.limits), **changes}).validate()
        self._refill()
        self.limits = new
        self._bucket = min(self._bucket, self.capacity)
        return new

    @property
    def active_jobs(self) -> int:
        return len(self._active)

    def _refill(self) -> None:
        now = self.clock()
        self._bucket = min(self.capacity, self._bucket + (now - self._last) * self.limits.max_share)
        self._last = now

    def _daily_used(self, now: float) -> float:
        while self._daily and self._daily[0][0] < now - 86400:
            self._daily.popleft()
        return sum(s for _, s in self._daily)

    def available_seconds(self) -> float:
        """Accelerator-seconds a new job could be granted right now (advertised to peers)."""
        self._refill()
        if self.limits.paused or len(self._active) >= self.limits.max_concurrent:
            return 0.0
        avail = self._bucket
        if self.limits.daily_seconds:
            avail = min(avail, self.limits.daily_seconds - self._daily_used(self.clock()))
        return max(0.0, avail)

    def admit(self, job_id: str, peer: str, requested_tokens: int, tokens_per_s: float) -> Grant:
        """Admit a job or raise Rejected. The returned grant's max_tokens may be clamped."""
        lim = self.limits
        now = self.clock()
        if lim.paused:
            return self._reject("node is paused by its owner")
        if len(self._active) >= lim.max_concurrent:
            return self._reject("node is at its concurrency limit")
        starts = self._peer_starts.setdefault(peer, deque())
        while starts and starts[0] < now - 60:
            starts.popleft()
        if len(starts) >= lim.per_peer_per_min:
            return self._reject("per-peer rate limit")
        if tokens_per_s <= 0:
            return self._reject("node has no measured throughput")
        budget = self.available_seconds()
        overhead = 1.0  # prompt processing / scheduling allowance, in seconds
        tokens = min(requested_tokens, lim.max_tokens_per_job)
        if tokens < requested_tokens:
            self.stats["tokens_clamped"] += 1
        # Admit if the budget covers at least a short reply; the job then runs until it finishes or
        # the budget runs out (deadline). Most replies are far shorter than their max_tokens.
        if budget < overhead + min(tokens, 32) / tokens_per_s:
            return self._reject("compute budget exhausted; retry later")
        seconds = min(budget, overhead + tokens / tokens_per_s)
        # Reserve now so concurrent admissions can't overspend; unused time is refunded on finish.
        self._bucket -= seconds
        starts.append(now)
        grant = Grant(job_id, peer, tokens, now + seconds, now, self)
        self._active[job_id] = grant
        self.stats["admitted"] += 1
        return grant

    # Swarm stages are charged per forward pass: compute time actually spent, not wall-clock,
    # because a stage waits idle between tokens while the rest of the chain works.
    def can_spend(self, seconds: float) -> bool:
        # Swarm slices have their own on/off switch; "paused" only makes the whole model private.
        self._refill()
        if self.limits.daily_seconds and self._daily_used(self.clock()) + seconds > self.limits.daily_seconds:
            return False
        return self._bucket >= seconds

    def charge(self, seconds: float) -> None:
        self._refill()
        self._bucket -= seconds
        self._daily.append((self.clock(), seconds))
        self.stats["seconds_used"] += seconds

    def _reject(self, reason: str):
        self.stats["rejected"] += 1
        raise Rejected(reason)

    def _release(self, grant: Grant) -> float:
        now = self.clock()
        self._active.pop(grant.job_id, None)
        reserved = grant.deadline - grant.started
        used = min(reserved, max(0.0, now - grant.started))
        self._refill()
        self._bucket = min(self.capacity, self._bucket + (reserved - used))  # refund unused reservation
        self._daily.append((now, used))
        self.stats["seconds_used"] += used
        return used

    def snapshot(self) -> dict:
        return {
            "limits": asdict(self.limits),
            "available_seconds": round(self.available_seconds(), 2),
            "capacity_seconds": round(self.capacity, 2),
            "active_jobs": len(self._active),
            "daily_seconds_used": round(self._daily_used(self.clock()), 1),
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }

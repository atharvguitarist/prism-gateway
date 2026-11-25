"""Per-provider circuit breaker.

Plain failover still *tries* a dead primary on every request (retries + backoff, or a full timeout)
before moving on. The breaker removes that tax: after `failure_threshold` consecutive failures a
provider is taken out of rotation for `cooldown_s`; then exactly one request is let through as a probe.

    closed ──N consecutive failures──▶ open ──cooldown elapsed──▶ half_open
       ▲                                │                             │
       └────────── probe succeeds ──────┼─────────────────────────────┘
                                        └──────── probe fails: back to open (cooldown restarts)

Design points
* State is per provider and lives in memory (single-instance by design, like the rate limiter).
* `before_call()` is the only place a probe is handed out, so two concurrent requests can never both
  probe. A probe that never reports back (client cancelled) expires after one more cooldown.
* The gateway fails *open*: if every provider in a chain is open, they are all tried anyway --
  a guaranteed 502 is never better than a long shot.
* Only failures that say something about provider health count (timeouts, connection errors, 5xx,
  malformed bodies, provider 429s). A provider 4xx is about the request, not about provider health.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass
class _State:
    state: str = CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probe_started: Optional[float] = None
    trips: int = 0
    skipped: int = 0
    last_error: Optional[str] = None


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 5.0, enabled: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self.enabled, self._clock = failure_threshold, cooldown_s, enabled, clock
        self._s: Dict[str, _State] = {}

    def _st(self, name: str) -> _State:
        return self._s.setdefault(name, _State())

    # ------------------------------------------------------------------ decisions
    def before_call(self, name: str) -> bool:
        """May a request be sent to `name` now? Hands out the single half-open probe."""
        if not self.enabled:
            return True
        s, now = self._st(name), self._clock()
        if s.state == CLOSED:
            return True
        if s.state == OPEN:
            if now - s.opened_at >= self.cooldown:
                s.state, s.probe_started = HALF_OPEN, now
                return True
            s.skipped += 1
            return False
        # half-open: one probe at a time; a probe that never reported back expires
        if s.probe_started is not None and now - s.probe_started < self.cooldown:
            s.skipped += 1
            return False
        s.probe_started = now
        return True

    def is_open(self, name: str) -> bool:
        """Raw state, ignoring cooldown expiry: did the circuit open (or re-open) and has not yet been probed?"""
        return self.enabled and self._st(name).state == OPEN

    def probing(self, name: str) -> bool:
        """True right after `before_call` admitted this request as the half-open probe."""
        return self.enabled and self._st(name).state == HALF_OPEN

    def state(self, name: str) -> str:
        s = self._st(name)
        if s.state == OPEN and self._clock() - s.opened_at >= self.cooldown:
            return HALF_OPEN          # would admit a probe on the next call
        return s.state

    # ------------------------------------------------------------------ outcomes
    def record_success(self, name: str) -> None:
        s = self._st(name)
        s.state, s.consecutive_failures, s.probe_started = CLOSED, 0, None

    def record_failure(self, name: str, kind: str = "error") -> bool:
        """Returns True when this failure tripped (or re-tripped) the breaker."""
        if not self.enabled:
            return False
        s, now = self._st(name), self._clock()
        s.last_error = kind
        if s.state == HALF_OPEN:                      # the probe failed
            s.state, s.opened_at, s.probe_started = OPEN, now, None
            s.trips += 1
            return True
        s.consecutive_failures += 1
        if s.state == CLOSED and s.consecutive_failures >= self.threshold:
            s.state, s.opened_at = OPEN, now
            s.trips += 1
            return True
        return False

    # ------------------------------------------------------------------ reporting
    def snapshot(self, names: List[str]) -> List[dict]:
        now, out = self._clock(), []
        for n in names:
            s = self._st(n)
            st = self.state(n)
            retry_in = max(0.0, self.cooldown - (now - s.opened_at)) if s.state == OPEN else 0.0
            out.append({"provider": n, "circuit": st, "consecutive_failures": s.consecutive_failures,
                        "trips": s.trips, "skipped_requests": s.skipped, "retry_in_s": round(retry_in, 1),
                        "last_failure_kind": s.last_error})
        return out

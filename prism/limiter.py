"""Race-safe sliding-window rate limiting (requests/min and tokens/min).

Why this is race-free
---------------------
`try_acquire` performs *check and consume in one critical section*:

    used = sum(window); if used + amount > limit: reject; else: window.append(amount)

There is no "read the counter, return, write it later" gap. The section contains no
`await`, so within the asyncio event loop it can never be interleaved with another
request; a `threading.Lock` additionally makes it safe if it is ever called from
threads (tests do). A read-then-write limiter would let N concurrent requests all
read "9 used" and all be admitted -- the classic over-admission bug the load test checks.

Window semantics: a true sliding window over the last `window_s` seconds (not a
fixed-minute bucket), so the guarantee is "never more than `limit` admissions in any
60-second span", with no burst-at-the-boundary loophole.

Limitation: state is in-process memory, i.e. correct for a single gateway instance.
Running several instances needs a shared store (e.g. Redis + a Lua script) -- documented as out of scope.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Tuple


@dataclass
class Decision:
    allowed: bool
    retry_after_s: float = 0.0
    used: int = 0
    limit: int = 0


class SlidingWindowLimiter:
    def __init__(self, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 max_idle_keys: int = 10_000):
        self.window_s = window_s
        self._clock = clock
        self._events: Dict[str, Deque[Tuple[float, int]]] = {}
        self._totals: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._max_idle_keys = max_idle_keys

    def _expire(self, name: str, now: float) -> Deque[Tuple[float, int]]:
        q = self._events.setdefault(name, deque())
        total = self._totals.get(name, 0)
        cutoff = now - self.window_s
        while q and q[0][0] <= cutoff:
            _, amt = q.popleft()
            total -= amt
        self._totals[name] = total
        return q

    def try_acquire(self, name: str, limit: int, amount: int = 1) -> Decision:
        """Atomically admit `amount` units if the window has room, else reject."""
        with self._lock:
            now = self._clock()
            q = self._expire(name, now)
            used = self._totals[name]
            if limit is not None and used + amount > limit:
                retry = 0.0
                if q:
                    # time until enough of the oldest events age out to fit `amount`
                    need = used + amount - limit
                    freed = 0
                    for ts, amt in q:
                        freed += amt
                        if freed >= need:
                            retry = max(0.0, ts + self.window_s - now)
                            break
                    else:
                        retry = max(0.0, q[-1][0] + self.window_s - now)
                return Decision(False, retry, used, limit)
            q.append((now, amount))
            self._totals[name] = used + amount
            return Decision(True, 0.0, used + amount, limit)

    def adjust(self, name: str, delta: int) -> None:
        """Correct the window after the fact (e.g. a token estimate vs. the real usage)."""
        if delta == 0:
            return
        with self._lock:
            now = self._clock()
            q = self._expire(name, now)
            q.append((now, delta))
            self._totals[name] = self._totals.get(name, 0) + delta

    def used(self, name: str) -> int:
        with self._lock:
            self._expire(name, self._clock())
            return self._totals.get(name, 0)

    def forget(self, name: str) -> None:
        with self._lock:
            self._events.pop(name, None)
            self._totals.pop(name, None)

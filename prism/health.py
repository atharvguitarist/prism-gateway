"""Rolling per-provider health, derived from live upstream attempts (not from a synthetic probe)."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple


class ProviderHealth:
    def __init__(self, window_s: float = 60.0, error_rate_threshold: float = 0.5, p95_threshold_ms: float = 5000,
                 clock: Callable[[], float] = time.time):
        self.window_s, self.err_thr, self.p95_thr, self._clock = window_s, error_rate_threshold, p95_threshold_ms, clock
        self._ev: Dict[str, Deque[Tuple[float, bool, int, Optional[str]]]] = {}
        self._last_error: Dict[str, Tuple[float, str]] = {}

    def record(self, provider: str, ok: bool, latency_ms: int, error_kind: Optional[str] = None) -> None:
        self._ev.setdefault(provider, deque(maxlen=5000)).append((self._clock(), ok, latency_ms, error_kind))
        if not ok:
            self._last_error[provider] = (self._clock(), error_kind or "error")

    def snapshot(self, providers: list[str]) -> list[dict]:
        now, out = self._clock(), []
        for p in providers:
            q = self._ev.get(p, deque())
            recent = [e for e in q if now - e[0] <= self.window_s]
            n, errs = len(recent), sum(1 for e in recent if not e[1])
            lat = sorted(e[2] for e in recent if e[1])
            p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None
            rate = errs / n if n else 0.0
            degraded = bool(n and (rate >= self.err_thr or (p95 is not None and p95 > self.p95_thr)))
            le = self._last_error.get(p)
            out.append({
                "provider": p, "window_s": self.window_s, "attempts": n, "errors": errs,
                "error_rate": round(rate, 3), "avg_latency_ms": round(sum(lat) / len(lat)) if lat else None,
                "p95_latency_ms": p95,
                "status": "no_traffic" if not n else ("degraded" if degraded else "healthy"),
                "last_error": {"kind": le[1], "seconds_ago": round(now - le[0], 1)} if le else None,
            })
        return out

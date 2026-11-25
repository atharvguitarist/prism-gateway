import asyncio
import threading

from prism.limiter import SlidingWindowLimiter


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_admits_up_to_limit_then_rejects_with_retry_after():
    clk = Clock(); lim = SlidingWindowLimiter(60, clk)
    assert all(lim.try_acquire("k", 3).allowed for _ in range(3))
    d = lim.try_acquire("k", 3)
    assert not d.allowed and 59 <= d.retry_after_s <= 60


def test_sliding_window_frees_capacity_as_events_age_out():
    clk = Clock(); lim = SlidingWindowLimiter(60, clk)
    lim.try_acquire("k", 2); clk.t += 30; lim.try_acquire("k", 2)
    assert not lim.try_acquire("k", 2).allowed
    clk.t += 31          # first event is now 61s old, second only 31s
    assert lim.try_acquire("k", 2).allowed and not lim.try_acquire("k", 2).allowed


def test_no_boundary_burst_loophole():
    """A fixed-minute bucket would admit 2x the limit across a boundary; a sliding window must not."""
    clk = Clock(); lim = SlidingWindowLimiter(60, clk)
    for _ in range(10): assert lim.try_acquire("k", 10).allowed
    clk.t += 59.9
    assert not lim.try_acquire("k", 10).allowed


def test_keys_are_independent():
    lim = SlidingWindowLimiter(60, Clock())
    assert lim.try_acquire("a", 1).allowed and not lim.try_acquire("a", 1).allowed and lim.try_acquire("b", 1).allowed


def test_token_amounts_and_adjust_refund():
    lim = SlidingWindowLimiter(60, Clock())
    assert lim.try_acquire("t", 100, 60).allowed
    assert not lim.try_acquire("t", 100, 60).allowed       # 120 > 100
    lim.adjust("t", -50)                                    # estimate was too high: 10 actually used
    assert lim.try_acquire("t", 100, 60).allowed


def test_zero_over_admission_under_thread_contention():
    lim = SlidingWindowLimiter(60)
    ok, lock = [], threading.Lock()
    def worker():
        r = lim.try_acquire("k", 10).allowed
        with lock: ok.append(r)
    ts = [threading.Thread(target=worker) for _ in range(200)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(ok) == 10


def test_zero_over_admission_under_asyncio_concurrency():
    lim = SlidingWindowLimiter(60)
    async def one():
        await asyncio.sleep(0)
        return lim.try_acquire("k", 25).allowed
    async def main():
        return await asyncio.gather(*[one() for _ in range(500)])
    assert sum(asyncio.run(main())) == 25

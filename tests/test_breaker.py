"""Circuit breaker: pure state-machine tests with a fake clock."""
from prism.breaker import CircuitBreaker


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def make(threshold=3, cooldown=5.0):
    c = Clock()
    return CircuitBreaker(threshold, cooldown, clock=c), c


def test_opens_after_consecutive_failures_only():
    b, _ = make()
    b.record_failure("a"); b.record_failure("a"); b.record_success("a")   # success resets the streak
    b.record_failure("a"); b.record_failure("a")
    assert b.state("a") == "closed" and b.before_call("a")
    assert b.record_failure("a") is True and b.state("a") == "open"
    assert b.before_call("a") is False


def test_open_circuit_skips_then_half_open_admits_exactly_one_probe():
    b, c = make()
    for _ in range(3): b.record_failure("a")
    c.t = 4.9
    assert b.before_call("a") is False
    c.t = 5.0
    assert b.state("a") == "half_open"
    assert b.before_call("a") is True        # the probe
    assert b.before_call("a") is False       # nobody else while it is in flight
    assert b.before_call("a") is False


def test_probe_success_closes_and_probe_failure_reopens_with_fresh_cooldown():
    b, c = make()
    for _ in range(3): b.record_failure("a")
    c.t = 5.0; assert b.before_call("a")
    b.record_failure("a")                    # probe failed
    assert b.state("a") == "open" and b.snapshot(["a"])[0]["trips"] == 2
    c.t = 9.9; assert b.before_call("a") is False   # cooldown restarted at t=5
    c.t = 10.0; assert b.before_call("a") is True
    b.record_success("a")
    assert b.state("a") == "closed" and b.before_call("a") and b.snapshot(["a"])[0]["consecutive_failures"] == 0


def test_lost_probe_expires():
    b, c = make()
    for _ in range(3): b.record_failure("a")
    c.t = 5.0; assert b.before_call("a")     # probe handed out, never reports back (client cancelled)
    c.t = 9.0; assert b.before_call("a") is False
    c.t = 10.0; assert b.before_call("a") is True


def test_providers_are_independent_and_disabled_breaker_never_blocks():
    b, _ = make()
    for _ in range(3): b.record_failure("a")
    assert b.before_call("a") is False and b.before_call("b") is True
    off = CircuitBreaker(enabled=False)
    for _ in range(10): off.record_failure("a")
    assert off.before_call("a") is True


def test_snapshot_reports_retry_in():
    b, c = make()
    for _ in range(3): b.record_failure("a", "timeout")
    c.t = 2.0
    s = b.snapshot(["a"])[0]
    assert s["circuit"] == "open" and s["retry_in_s"] == 3.0 and s["last_failure_kind"] == "timeout"

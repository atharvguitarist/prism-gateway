# Contributing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test          # 58 tests, ~3 s, no network
make mocks         # alpha :9001, beta :9002 (background)
make run           # gateway on :8080
make demo          # asserted 10-step demo
```

## Ground rules

1. **Money is an integer** (nano-USD). Never add floats to a running total.
2. **No `await` inside the limiter's critical section.** That property is what makes it race-free.
3. **Cache lookups must never iterate another tenant's entries.** Add a test for any change near `cache.py`.
4. **Never splice provider output in a stream.** Fail loudly with an SSE `error` event and no `[DONE]`.
5. **Every upstream call has a timeout.**
6. New behaviour needs a test through the in-process fake provider (`tests/conftest.py`); use the real-socket `live_server` fixture only for streaming and concurrency.
7. Routing changes must keep `python -m prism.routing_eval` and the held-out set green, and must not read the `expected_tier` labels.

Run `python3 scripts/verify.py` before a release: it rebuilds `VERIFICATION_REPORT.md` and `reports/` from a clean state.

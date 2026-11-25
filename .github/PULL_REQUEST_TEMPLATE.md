## What and why

## Checklist
- [ ] `make test` passes
- [ ] New behaviour has a test (fake provider; real socket only for streaming/concurrency)
- [ ] Money stays integer nano-USD; no `await` inside the limiter critical section
- [ ] Cache isolation untouched, or a test added
- [ ] README / docs updated if behaviour or config changed

<p align="center"><img src="docs/images/banner.svg" alt="Prism — LLM gateway and semantic cache" width="100%"></p>

<p align="center">
  <a href="https://github.com/atharvguitarist/prism-llm-gateway/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/atharvguitarist/prism-llm-gateway/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white">
  <img alt="Starlette" src="https://img.shields.io/badge/Starlette-async-009688">
  <img alt="SQLite WAL" src="https://img.shields.io/badge/SQLite-WAL-003b57?logo=sqlite&logoColor=white">
  <img alt="Docker Compose" src="https://img.shields.io/badge/docker-compose-2496ed?logo=docker&logoColor=white">
  <img alt="58 tests" src="https://img.shields.io/badge/tests-58%20passing-3ecf8e">
  <img alt="OpenAI compatible" src="https://img.shields.io/badge/API-OpenAI--compatible-7c6cff">
  <img alt="MIT" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

<p align="center">
  <b><a href="#-quick-start">Quick start</a></b> ·
  <b><a href="#-how-a-request-flows">How it flows</a></b> ·
  <b><a href="#-ops-console">Console</a></b> ·
  <b><a href="#-api">API</a></b> ·
  <b><a href="#-architecture">Architecture</a></b> ·
  <b><a href="docs/REQUIREMENTS_TRACEABILITY.md">Requirements</a></b> ·
  <b><a href="VERIFICATION_REPORT.md">Evidence</a></b>
</p>

> **Prism** is an OpenAI-compatible gateway that sits between applications and model providers and handles what every AI product ends up needing: **virtual keys, budgets and rate limits, streaming, retries and provider failover, a per-tenant semantic cache, per-request cost metering, and difficulty-based smart routing** (`"model": "auto"`), plus an ops console. Built for the Airtribe *AI-first Software Engineering* capstone (Prism case study).

### At a glance

| 🔐 Control | ⚡ Performance | 🛡️ Resilience | 📊 Visibility |
|---|---|---|---|
| Virtual keys · allowlists · monthly budgets · req/min + tokens/min | Per-tenant semantic cache (hits cost **$0**) · true SSE pass-through · `auto` routing to the cheapest capable tier | Timeouts · retry with backoff · ordered failover · never splices a broken stream | Exact nano-USD metering · full request log · usage API · live ops console |

<table align="center"><tr>
<td align="center"><b>58</b><br><sub>automated tests</sub></td>
<td align="center"><b>18 / 18</b><br><sub>provided smoke checks</sub></td>
<td align="center"><b>0</b><br><sub>over-admissions under load</sub></td>
<td align="center"><b>20 / 20</b><br><sub>routing eval (provided)</sub></td>
<td align="center"><b>3</b><br><sub>runtime dependencies</sub></td>
</tr></table>

Every **Must Have** in the brief is implemented, plus several Good-To-Have items ([list](#whats-implemented)). Evidence for each claim is in **[VERIFICATION_REPORT.md](VERIFICATION_REPORT.md)**, regenerated from a clean run by `scripts/verify.py`; the requirement-by-requirement map is in **[docs/REQUIREMENTS_TRACEABILITY.md](docs/REQUIREMENTS_TRACEABILITY.md)**.

- **Stack:** Python 3.12 · Starlette + uvicorn (async) · httpx · SQLite (WAL). No paid AI provider needed — the two provided mock providers are the upstreams.

## 🔭 How a request flows

<p align="center"><img src="docs/images/pipeline.svg" alt="Animated request pipeline" width="100%"></p>

<sub>The animation shows a cache **miss** (green), a cache **hit** that skips the upstream (blue), and a provider failure that retries and fails over (orange).</sub>

<details>
<summary><b>Sequence view: what happens when the primary provider is down</b></summary>

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Prism
    participant K as Cache
    participant A as alpha (primary)
    participant B as beta (fallback)
    participant S as SQLite
    C->>G: POST /v1/chat/completions (Bearer key, model=fast)
    G->>G: auth, validate, allowlist, budget, rate limit
    G->>K: lookup(key, tier, last user message)
    K-->>G: miss
    G->>A: chat (timeout 2 s)
    A--xG: 503
    G->>A: retry after 200 ms
    A--xG: 503
    G->>B: failover
    B-->>G: 200 + usage
    G->>S: one transaction: log row + usage bump
    G->>K: put(answer)
    G-->>C: 200 · x-prism-provider: beta/beta-small · x-prism-fallback: true · x-prism-cost-usd
```

</details>

<details>
<summary><b>Decision tree: how failures are handled</b></summary>

```mermaid
flowchart TD
    S([upstream attempt]) --> R{outcome}
    R -->|200 ok| OK([meter, log, return])
    R -->|5xx / connection / malformed body| RT{attempts < 3?}
    RT -->|yes| BO[backoff 200ms x 2^n] --> S
    RT -->|no| FO
    R -->|timeout| FO[fail over to next provider]
    R -->|provider 429| FO
    R -->|other 4xx| FO
    FO --> MORE{another provider in chain?}
    MORE -->|yes| S
    MORE -->|no or max_total_ms hit| E502([502 upstream_error])
    subgraph Streaming
      ST{{first chunk already sent?}} -->|no| FO
      ST -->|yes| ERR([SSE error event, end WITHOUT DONE - never splice])
    end
```

</details>

<details>
<summary><b>Developer workflow: from commit to verified release</b></summary>

```mermaid
flowchart LR
    DEV[edit code] --> T[make test<br/>58 tests, 3 s] --> RE[routing eval<br/>provided + held-out]
    RE --> V[scripts/verify.py<br/>fresh state, all checks] --> REP[VERIFICATION_REPORT.md]
    DEV -->|push| CI{{GitHub Actions}}
    CI --> J1[pytest + routing eval]
    CI --> J2[docker compose up --build --wait] --> DEMO[scripts/demo.py<br/>asserted 10-step drill]
```

</details>

---

## 🚀 Quick start

### Option A — Docker (recommended)

```bash
docker compose up --build -d          # alpha (:9001), beta (:9002), prism (:8080)
open http://localhost:8080/console    # ops console — admin token: prism-admin-demo
python3 scripts/demo.py --pause       # guided 10-step demo (stdlib only, any Python 3.9+)
docker compose down -v                # stop and wipe state (resets budgets / rate limits)
```

### Option B — local, no Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# three terminals (or use `make mocks` + `make run`)
python3 scripts/mock_provider.py --port 9001 --name alpha
python3 scripts/mock_provider.py --port 9002 --name beta
.venv/bin/uvicorn prism.app:app --factory --port 8080

python3 scripts/demo.py               # or: make demo
```

### Try it by hand

```bash
curl -i http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer prism-sk-search-1a2b3c" -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "What is a message queue?"}]}'
# → x-prism-provider: alpha/alpha-small   x-prism-cache: miss   x-prism-fallback: false   x-prism-cost-usd: 0.00001395
```

Because it is OpenAI-compatible, the official SDKs work by pointing `base_url` at `http://localhost:8080/v1` and using a virtual key as the API key.

### Seeded demo keys (`data/seed_keys.json`)

| Team | Key | Budget / mo | Rate limit | Allowlist | Cache |
|---|---|---|---|---|---|
| search | `prism-sk-search-1a2b3c` | $50 | 60 req/min | `fast` | on, ≥ 0.92 |
| research | `prism-sk-research-4d5e6f` | $500 | 300 req/min | `fast` `smart` `auto` | off |
| free-tier | `prism-sk-free-7g8h9i` | $5 | **10 req/min** | `fast` | on, ≥ 0.85 |
| budget-demo | `prism-sk-budget-demo-0j1k2l` | **$0.00001** | 60 req/min | `fast` | off |

Aliases: `fast` → `alpha-small` (fallback `beta-small`), `smart` → `alpha-large` (fallback `beta-large`), `auto` → classified into one of those (`data/gateway_config.sample.json`).

---

## 🖥️ Ops console

`http://localhost:8080/console` is a self-contained dashboard (no CDN, no build step): **Overview** (KPI tiles, live traffic and latency charts, cache donut, spend vs budget, provider health), **Playground** (send real requests with any key and model, stream over SSE, see the `x-prism-*` headers), **Virtual keys** (create, disable, enable, rotate, copy), **Requests** (filterable log) and **Routing & cache** (per-key hit rates, auto-routing reasons, one-click router eval). Dark and light themes; keyboard shortcuts `r` and `1`–`5`.

| Overview | Playground |
|---|---|
| ![Console overview](docs/images/console-overview.png) | ![Console playground](docs/images/console-playground.png) |

![Request log with rate-limit rejections, cache hits and a budget rejection](docs/images/console-requests.png)

---

## ⚙️ Configuration

<details>
<summary><b>Environment variables and gateway-config options</b></summary>

| Variable | Default | Meaning |
|---|---|---|
| `PRISM_ADMIN_TOKEN` | `prism-admin-demo` | Admin-plane token (`X-Admin-Token` header). **Change it outside a local demo.** |
| `PRISM_CONFIG` | `data/gateway_config.sample.json` | Providers, aliases, fallbacks, retry policy. Docker uses `config/gateway_config.docker.json`. |
| `PRISM_DATA_DIR` | `data` | Where `model_pricing.json` and `seed_keys.json` live. |
| `PRISM_DB` | `prism.sqlite3` | SQLite file (keys, usage, request log, cache entries). Docker: `/data/prism.sqlite3` on a named volume. |
| `PRISM_UPSTREAM_TIMEOUT_MS` | `2000` | Per-attempt upstream timeout (also time-to-first-chunk for streams). **Raise it for real LLM providers.** |
| `PRISM_STREAM_IDLE_TIMEOUT_MS` | `5000` | Max silence between chunks of an in-progress stream. |
| `PRISM_PROVIDER_<NAME>_API_KEY` / `_BASE_URL` | from config | Override a provider's secret/URL without touching files, e.g. `PRISM_PROVIDER_ALPHA_API_KEY`. |

Optional sections in the gateway config JSON: `retry` (`max_attempts`, `initial_backoff_ms`, `backoff_multiplier`, `max_total_ms`), `timeouts`, `cache` (`ttl_seconds`, `max_entries_per_key`, `default_threshold`), `escalation.enabled`, `circuit_breaker` (`enabled`, `failure_threshold` = 3, `cooldown_s` = 5), `guardrails` (`pii`: `off`\|`redact`\|`block`, default `redact`; `injection`: `off`\|`flag`\|`block`, default `flag`). A model belongs to the provider whose name prefixes it (`alpha-small` → `alpha`), or list models explicitly with `"models": [...]` on a provider. Provider API keys are gateway secrets: never returned to callers, never written to logs, excluded from `repr()`.

Seed tenants are inserted **only if absent**, so keys disabled/rotated through the admin API stay that way across restarts.

</details>


---

## 🔌 API

### Data plane — `POST /v1/chat/completions`

OpenAI-compatible request/response. `"model"` is an alias (`fast`, `smart`, `auto`) or a concrete model name the key's allowlist permits (a concrete model is *pinned*: no fallback chain). `"stream": true` returns `text/event-stream` ending in `data: [DONE]`.

**Response headers (every response, including errors):**

| Header | Value |
|---|---|
| `x-prism-provider` | `provider/model` that produced the content (`none` on rejections; on a cache hit, the origin of the cached answer) |
| `x-prism-cache` | `hit` or `miss` |
| `x-prism-fallback` | `true` only when a non-primary provider served it |
| `x-prism-cost-usd` | cost of *this* request in USD (`0` on a hit). **Streaming:** omitted — the cost is unknown until the final usage chunk; it is still logged and counted in the usage API |
| `x-prism-request-id` | correlates with `/admin/logs` |
| `x-prism-route`, `x-prism-route-reason` | `auto` requests: chosen tier and why |
| `x-prism-cache-similarity` | on hits: cosine similarity that matched |
| `x-prism-escalated` | `true` when a refusal from the cheap tier was retried on `smart` |

**Errors** (OpenAI-shaped `{"error": {"message", "type", "code"}}`, each distinguishable by `type`):

| Case | HTTP | `type` |
|---|---|---|
| Missing / unknown / disabled key | 401 | `authentication_error` |
| Malformed body / empty `messages` | 400 | `invalid_request_error` |
| Unknown model or alias | 404 | `not_found_error` |
| Model not on the key's allowlist | 403 | `model_not_allowed` |
| Monthly budget exhausted | 402 | `budget_exceeded` |
| Rate limit exceeded (`Retry-After` set) | 429 | `rate_limit_exceeded` |
| All providers failed (code: `upstream_timeout`, `upstream_http`, …) | 502 | `upstream_error` |

### Admin plane (header `X-Admin-Token: <token>`)

| Endpoint | Purpose |
|---|---|
| `GET /health[?deep=1]` | liveness (public); `deep` also pings each provider |
| `GET /admin/usage?key=&from=&to=` | spend and tokens for a key over a `YYYY-MM-DD` window (default: this month): `requests`, `rejected`, `errors`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `cache_hits`, `by_model`, `by_day`. Without `key`: per-key rollup + totals |
| `GET /admin/logs?key=&limit=&status=&routed=1` | request log, newest first (`routed=1` → only `auto` decisions) |
| `GET /admin/cache/stats[?key=]` | lookups, hits, hit rate, cost saved, entries, per-key breakdown |
| `GET /admin/providers/health` | per-provider error rate / latency from live traffic, plus circuit state (`circuit`, `retry_in_s`, `trips`); gateway added-latency percentiles |
| `GET /metrics` | Prometheus text exposition (requests by outcome, tokens, spend, cache, guardrail events, circuit state, provider health, per-key budget use) |
| `GET /admin/config` | effective gateway config (provider secrets are never included) |
| `PATCH /admin/keys/{key}` | partial key update, applied live: `team`, `monthly_budget_usd`, `rate_limit`, `model_allowlist`, `semantic_cache` |
| `DELETE /admin/cache[?key=]` | purge one key's cache entries, or all of them |
| `GET /admin/routing/eval[?file=]` | run the routing eval over `data/<file>` |
| `GET/POST /admin/keys`, `POST /admin/keys/{key}/disable\|enable\|rotate` | key management |
| `GET /console` | ops console (a static page that calls the endpoints above) |

The request log records, for **every** request including rejected ones: request id, key, requested model, resolved provider/model, status, tokens, cost, cache result, fallback flag, escalation flag, `route_reason`, retries, latency, upstream time and gateway overhead. It never stores prompt or response bodies.

---

## 🏗️ Architecture

```mermaid
flowchart LR
    C[Client / SDK] -->|virtual key| G
    subgraph G[Prism gateway - one async process]
      direction TB
      A[1 auth] --> V[2 validate] --> R[3 resolve alias] --> L[4 allowlist] --> B[5 budget] --> RL[6 rate limit]
      RL --> AU{auto?}
      AU -->|yes| CL[7 difficulty classifier] --> CA
      AU -->|no| CA[8 semantic cache, per key]
      CA -->|hit: $0| OUT
      CA -->|miss| UP[9 upstream call: timeout, retry+backoff, failover]
      UP --> M[10 meter + log, atomic] --> OUT[response + x-prism-* headers]
    end
    UP --> P1[(alpha)]
    UP --> P2[(beta)]
    M --> DB[(SQLite: keys, usage, request log, cache)]
    OPS[Ops console] -->|/admin/*| DB
```

```
prism/
  app.py          Starlette app, routes, admin plane, lifespan wiring
  gateway.py      the request pipeline (data plane), streaming, retries/failover, escalation
  providers.py    ProviderAdapter interface + OpenAI-compatible HTTP adapter (swap in mocks/fakes/real providers)
  smart_router.py difficulty classifier behind `auto`        routing_eval.py  one-command eval
  cache.py        per-key semantic cache                      embedding.py     local lexical-concept embedder
  limiter.py      race-safe sliding-window limiter            pricing.py       exact integer cost math
  store.py        SQLite persistence                          health.py        rolling provider health
  config.py       settings + gateway config                   console/         ops console (static HTML)
tests/            58 tests: unit + integration (fake provider) + real-socket streaming/concurrency tests
scripts/          provided mock/smoke/load scripts + demo.py (10-step demo) + verify.py (report generator)
```

### Request pipeline and check order

Auth → body validation → alias resolution → allowlist → guardrails (PII redaction / injection screening) → budget → rate limit → (`auto` classification) → cache → upstream. The order is deliberate: cheap, certain rejections come first; **the rate-limit slot is consumed only after budget passes**, so a key that is over budget cannot also burn its request quota; an `auto` request is classified only after it is admitted. Each rejection type has its own status and `type`.

### Design decisions

<details>
<summary><b>Design decisions — routing, retries, streaming, cache, metering, limiter (click to expand)</b></summary>

**Routing and aliases.** `fast`/`smart` resolve to an ordered chain `[primary, *fallbacks]` of models; each model maps to a provider through an adapter. Callers never see provider credentials or need provider model names. Concrete model names are allowed only if on the key's allowlist and are pinned (no fallbacks).

**Retries and failover (per attempt: hard timeout).** Failures are classified once, in the adapter, into `retry_same` / `failover` flags:

| Upstream outcome | Behaviour |
|---|---|
| 5xx, connection error, malformed 200 body | retry the *same* provider, exponential backoff (200 ms × 2ⁿ, up to 3 attempts), then fail over |
| **Timeout** (default 2 s) | **no retry** — a slow provider will likely stay slow; fail over immediately |
| 429 from provider | fail over immediately |
| other 4xx | no retry; try the next provider (likely a per-provider config problem) |
| chain exhausted, or `max_total_ms` (15 s) exceeded | `502 upstream_error` |

Failover sets `x-prism-fallback: true`, and `retries` in the log counts failed upstream attempts. Recovery is immediate: the next request goes to the primary again (health-window-based recovery is not implemented; `/admin/providers/health` is observational).

**Streaming.** Upstream chunks are forwarded as they arrive — no buffering (a test holds the provider mid-stream and asserts the client already received chunk 1). Failover can happen only *before the first chunk is delivered*; once bytes have reached the client the stream is committed. **Mid-stream failure:** the gateway emits an SSE `error` event (`code: stream_interrupted`) and **ends the stream without `[DONE]`**, so a truncated answer can never look complete. It does not restart on another provider and splice outputs. Usage is read from the provider's final usage chunk and metered like any request; if a provider omits it, tokens are estimated from word count and the log row says so. A client disconnect is logged as `stream_aborted`.

**Cache scoping and semantics (data-leak safety).** The cache index is keyed `(virtual_key, scope)`: a lookup *only iterates that tenant's entries* — other tenants' entries are never examined, so even a threshold of 0 cannot leak. `scope` is the resolved tier, so a `fast` answer is never served to a `smart` request. The last user message is matched semantically; the **rest of the conversation (system prompt + earlier turns) must match exactly** (context hash), which is the documented answer to the multi-turn question. Per-key `enabled` / `similarity_threshold` come from the seed data. Never cached: errors, refusals, non-`stop` finishes, tool/function calls, `n > 1`, and time-sensitive prompts (`current`, `status`, `today`, `latest`, …). Eviction: 24 h TTL + per-key LRU cap of 500. Cache hits cost `$0`, return zeroed `usage`, and are logged with the cost they saved. Streaming requests *fill* the cache; a streamed hit is replayed as a single content chunk followed by `[DONE]`.

**Embedding choice (important caveat).** The matcher is a documented *local substitute*, not a neural model: lowercase → canonicalise a few phrases (“money back” → refund, “two-factor” → 2fa) → drop stop-words/question scaffolding → stem → map a small synonym lexicon → weight generic words down → cosine on sparse vectors (`prism/embedding.py`). Negations are kept as tokens. It meets the brief's pairs (paraphrase A ≈ 1.0, B ≈ 0.97, near-miss ≈ 0.58; see the report) but it cannot match paraphrases that share no vocabulary beyond its lexicon. `Embedder` is a one-method protocol — a real embedding model drops in without touching the cache.

**Smart routing (`auto`).** Technique: an offline engineered-signal scorer (`prism/smart_router.py`), ~0.1 ms, deterministic, no model call. The central idea is **classify the task, not the prompt**: long prompts are scored on their first sentence + last two sentences only, so pasted context can't inflate difficulty, and short proofs can't hide. Hard signals (proof/derive, estimation, design, migration/rollback, comparison + recommendation, diagnosis, distributed-systems vocabulary, algorithmic complexity, code-writing, multi-part asks) add points; easy signals (lookup, convert/translate/extract/rewrite, one-liner, arithmetic, definition) subtract; `score ≥ 2.0 → smart`. Every decision logs its score and matched signals (`route_reason`, also in the `x-prism-route-reason` header). If a key allows `auto` but not the chosen tier, the request is downgraded to an allowed tier (and the reason says so). `expected_tier` labels are never read by the router. Accuracy numbers, with an honest account of overfitting risk, are in the verification report.

**Metering and budgets.** Cost = provider-reported `usage` × the price table (never client-declared tokens), computed in **integer nano-USD** so that the number in the `x-prism-cost-usd` header is exactly what is added to the totals and thousands of requests sum with zero floating-point drift. Each request writes its log row and bumps the `(key, month)` usage row in **one SQLite transaction**; budget enforcement is a single primary-key read of that row (never a log scan). Window = UTC calendar month; a new month has no row yet, which *is* the reset-on-read. **Documented overshoot:** admission checks `spent < budget` and the cost is added on completion, so requests in flight at the same moment can each be admitted and the budget can be overshot by at most the cost of those in-flight requests (and a single stream admitted with $0.0000001 left may cost more than that). The budget-demo key makes this visible: it admits one request and rejects the next.

**Rate limiting.** True sliding window (no fixed-bucket boundary burst) for requests/min and tokens/min, with check-and-consume in one critical section and no `await` inside it — so under concurrent load it is race-free, not read-then-write. Token usage is pre-charged with a word-count estimate and corrected to the real usage afterwards; a request rejected on tokens gives its request slot back.

**Persistence.** Keys, usage, request log and cache entries live in SQLite (WAL) and survive restarts (tested). Rate-limit windows are in memory (reset on restart; allowed for single-instance by the brief).

**Provider adapter.** The gateway depends only on `ProviderAdapter` (`chat`, `open_stream`); `OpenAICompatAdapter` is the HTTP implementation, and the test suite drives the entire pipeline through an in-process fake — that is also how mid-stream failures are tested.

**Secrets.** Provider keys are never in responses, errors, logs or the console (asserted by a test that drives an all-providers-down failure and greps every surface). Unknown virtual keys sent by callers are not persisted (they may be mistyped real secrets).

</details>

### What's implemented

*Must Have:* provided data loaded · unified OpenAI-compatible API over 2 providers with the header contract · streaming pass-through · virtual keys with allowlists / rate limits / budgets, distinct errors · aliases, retries, backoff, failover · `auto` smart routing + one-command eval · per-key semantic cache · metering, request log, usage API · ops console · passes `smoke_test.py`, zero over-admission on `load_test.py` · automated tests for cost math, limiter, cache decisions, allowlist, routing.

*Good To Have, also done:* response-quality escalation (cheap-tier refusal → retried on `smart`, both calls billed, only for keys allowed `smart`) · provider health endpoint · tokens-per-minute limits · key-management APIs (create / disable / enable / rotate) · usage by model and by day · cache TTL and cached answers replayed as SSE · added-latency percentiles.

*Also done:* per-provider circuit breaker (a dead primary is skipped after 3 consecutive failures, probed once per cooldown, fail-open if every circuit is open; `x-prism-circuit-skipped` header) · guardrails (PII / secret redaction before the cache and the provider, injection flagging, optional blocking; `x-prism-guardrail` header and `flags` in the request log) · `x-prism-budget-remaining-usd` header · Prometheus `/metrics` · live key updates, cache purge and config view in the admin plane.

*Not done:* distributed rate limiting, weighted/least-latency routing, a second wire format, cost-anomaly alerts, orgs.

---

## ✅ Testing and verification

```bash
make test                          # 58 tests, ~3 s, no network (fake providers + a real-socket uvicorn for streaming)
python3 scripts/smoke_test.py --url http://localhost:8080 --key prism-sk-search-1a2b3c --model fast
python3 scripts/load_test.py  --url http://localhost:8080 --key prism-sk-free-7g8h9i --model fast --requests 30 --concurrency 10 --rpm-limit 10
python3 -m prism.routing_eval                                      # provided 20-case set
python3 -m prism.routing_eval --file data/routing_eval_heldout.jsonl   # my 30-case held-out set
python3 scripts/verify.py          # everything above on fresh state → VERIFICATION_REPORT.md
```

The tests were checked by mutation: deliberately buffering the stream, sharing the cache across tenants, and turning the limiter into read-then-write each make a specific test fail.

`data/routing_eval_heldout.jsonl` is **added data** (not part of the provided pack): 30 prompts I wrote with the same trap structure, used only to estimate generalisation of the router. It does not affect any other verification.

### Demo (matches the brief's recommended flow)

`python3 scripts/demo.py --pause` steps through all ten items (healthy providers → headers → streaming → cache hit on a paraphrase + near-miss + tenant isolation → `auto` routing + eval accuracy → rate-limit burst → budget rejection → live failover, slow provider, flaky provider, recovery → console → smoke test), asserting each result. Add `--no-salt` on a fresh database to use the exact sample prompts. Inject failures yourself with `curl -X POST localhost:9001/admin/config -d '{"mode":"down"}'`.

---

## ⚠️ Known limitations

<details>
<summary><b>Honest limitations (click to expand)</b></summary>

- **Single instance.** Rate-limit and in-flight state are in process memory; run **one worker** (the Dockerfile does). Multi-instance needs a shared store (e.g. Redis with a Lua script); the SQLite store is also single-node.
- **Budget overshoot** is bounded by in-flight cost, not zero (see above). Budget is checked before the cache, so an exhausted key is rejected even for would-be free cache hits.
- **Cache embedder is lexical, not neural** — see the caveat above; a prompt pair with no shared vocabulary beyond the synonym lexicon will miss, and unusual domain words are matched literally. Sampling parameters (temperature, max_tokens) are not part of the cache key.
- **Router is heuristic.** It can misroute prompts that don't resemble the signal vocabulary, and a *definitional* question that happens to contain systems words (“Explain what a distributed system is”) will score `smart`. Accuracy on the provided set is a development-set number (see the report).
- **Failover takes a few hundred ms to ~2 s** per request while a provider is dead (retry backoff for “down”, the timeout for “slow”) because health is observed, not used to pre-emptively skip a provider.
- **Default timeouts are mock-sized** (2 s). Real LLMs need a larger `PRISM_UPSTREAM_TIMEOUT_MS`.
- **Mid-stream failure ends the stream with an error event** (no automatic retry, by design); the partial output is billed only if the provider had already reported usage.
- **Console auth is just the admin token** typed into the page (polling, no sessions or roles).
- **Docker:** the `Dockerfile` and `docker-compose.yml` were authored without access to a Docker daemon, so the image build was not run by me. The same commands (`pip install -r requirements.txt`, the same `uvicorn` command, the same mock-provider commands and config) were executed natively and verified; CI (`.github/workflows/ci.yml`) builds the image and runs the demo drill against the compose stack on every push, which is the authoritative check.

</details>

## Provided starter pack

The case-study files (`docs/PRISM_PROBLEM_STATEMENT.md`, `docs/API_CONTRACT.md`, `docs/DATA_MODEL.md`, `docs/EVALUATION_GUIDE.md`, `docs/IMPLEMENTATION_GUIDE.md`, `docs/STARTER_PACK_README.md`), the data files in `data/`, and `scripts/{mock_provider,smoke_test,load_test,validate_pack}.py` are the unmodified starter pack from `airtribe-projects/prism-capstone`.

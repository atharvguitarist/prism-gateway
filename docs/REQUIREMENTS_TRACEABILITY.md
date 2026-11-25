# Requirements traceability

Every requirement in the capstone brief (`docs/PRISM_PROBLEM_STATEMENT.md`), mapped to **where it is implemented**, **which automated test pins it**, and **where the evidence is**. Test names refer to `tests/`; evidence files are produced by `python3 scripts/verify.py` and live in [`reports/`](../reports) and [`VERIFICATION_REPORT.md`](../VERIFICATION_REPORT.md).

## Must Have

| # | Requirement | Implementation | Pinned by tests | Evidence |
|---|---|---|---|---|
| 1 | Load provided price table, seed tenants, gateway config; keep seed keys and alias names | `config.py`, `store.py` (seed-if-absent), `pricing.py` | `test_usd_roundtrip_for_seed_budget`, `test_budget_survives_restart` | `reports/smoke_test.txt` |
| 2a | One OpenAI-compatible `POST /v1/chat/completions` over ≥ 2 providers | `app.py`, `gateway.py`, `providers.py` | `test_header_contract_and_openai_shape` | smoke test 18/18 |
| 2b | Unknown model → clear 4xx; upstream errors → clean gateway errors; provider credentials never exposed | `gateway.py` (resolve), `providers.classify_status` | `test_distinct_rejections`, `test_all_providers_down_is_a_clean_502`, `test_malformed_upstream_is_handled`, `test_provider_secrets_never_leak` | report §errors |
| 2c | Header contract `x-prism-provider / cache / fallback / cost-usd` (cost optional on streams) | `gateway._headers` | `test_header_contract_and_openai_shape`, `test_stream_shape_metering_and_headers` | smoke test |
| 3a | Streaming pass-through, no buffering | `gateway._serve_stream` | `test_streaming_is_passed_through_without_buffering` (provider held mid-stream; client must already hold chunk 1) | demo step 3 |
| 3b | Terminates with `[DONE]`; usage/cost recorded for streams | `gateway._finish_stream` | `test_stream_shape_metering_and_headers`, `test_stream_without_usage_is_estimated_and_flagged` | usage API check in report |
| 4a | Virtual-key auth on every request | `gateway` step 1 | `test_distinct_rejections`, `test_invalid_key_value_is_not_persisted` | — |
| 4b | Per-key model allowlist, req/min limit, monthly budget; each rejection distinct | `gateway` steps 4–6, `limiter.py` | `test_distinct_rejections`, `test_rate_limit_is_enforced_and_logged`, `test_budget_exhaustion_first_request_consumes_second_is_rejected` | demo steps 6–7 |
| 4c | Race-safe limiter (no read-then-write) | `limiter.SlidingWindowLimiter.try_acquire` (one critical section, no `await`) | `test_zero_over_admission_under_thread_contention`, `test_zero_over_admission_under_asyncio_concurrency`, `test_no_boundary_burst_loophole` | `load_test.py`: exactly 10 / 30 admitted at 10 rpm |
| 5a | Aliases → primary + ordered fallbacks | `config.GatewayConfig`, `gateway._run_chain` | `test_failover_when_primary_down_sets_header_and_log` | demo step 8 |
| 5b | Timeouts on every upstream call; retries with backoff on transient errors | `providers.py`, `gateway._run_chain` | `test_timeout_and_ratelimit_fail_over_immediately_without_retry`, `test_transient_errors_are_absorbed_by_retry` | report §failure drills |
| 5c | Mid-stream failure policy decided and documented | `gateway._serve_stream` (SSE `error` event, no `[DONE]`, never splice providers) | `test_mid_stream_failure_emits_error_event_never_splices_providers`, `test_stream_failover_before_first_byte` | README → Streaming |
| 6a | `auto` difficulty router graded on the provided eval | `smart_router.py` | `test_provided_eval_set_beats_length_baseline_by_a_wide_margin`, `test_the_traps`, `test_router_never_reads_labels` | `reports/routing_eval_provided.txt` (20/20; length-only baseline 8/20) |
| 6b | One-command eval + logged reason for each decision | `routing_eval.py`, `gateway` (`route_reason`, `x-prism-route-reason`) | `test_auto_routes_by_difficulty_and_logs_reason`, `test_decision_carries_a_reason`, `test_routing_eval_endpoint_and_provider_health` | `reports/routing_eval_heldout.txt` (30/30, self-written) |
| 7a | Per-key semantic cache; provided paraphrase pairs hit | `cache.py`, `embedding.py` | `test_pair_a_…`, `test_pair_b_…`, `test_hit_on_paraphrase_miss_on_other_intent`, `test_semantic_cache_hit_on_paraphrase_costs_zero` | demo step 4 |
| 7b | **No cross-tenant leakage** | index keyed `(virtual_key, scope)`; lookups never iterate other tenants | `test_cache_is_isolated_between_tenants_and_scopes`, `test_cache_never_leaks_across_tenants` | demo step 4 |
| 7c | Don't cache errors / time-sensitive prompts; multi-turn handled | `cache.is_cacheable_request`, `is_refusal`, `context_hash` | `test_errors_are_not_cached`, `test_what_is_not_cacheable`, `test_cache_disabled_key_and_time_sensitive_prompts_are_never_cached`, `test_multiturn_context_must_match_exactly`, `test_negation_is_not_ignored` | — |
| 8a | Cost from provider `usage` × price table, never client tokens | `pricing.PriceTable.cost_nusd` (integer nano-USD) | `test_cost_from_price_table_is_exact`, `test_cost_has_no_float_drift_when_summed` | report §accounting |
| 8b | Request log for every request, including rejections | `store.record_request` (log row + usage bump, one transaction) | `test_every_request_including_rejections_is_logged`, `test_accounting_is_exact_and_limiter_race_free_under_concurrent_load` | 300 concurrent requests: usage API equals client totals to the nano-dollar |
| 8c | Usage API | `app.usage`, `store.usage_summary` | `test_accounting_is_exact_…`, `test_key_management_create_disable_rotate` | report §usage |
| 9 | Ops console: usage by key, recent requests, cache hit rate | `prism/console/index.html` (served at `/console`) | `test_admin_requires_token_and_serves_console` | demo step 9 |
| 10 | Passes provided `smoke_test.py`; `load_test.py` shows zero over-admission | — | CI + `scripts/verify.py` | `reports/smoke_test.txt` |

## Deliverables

| Brief deliverable | Where |
|---|---|
| GitHub repository | this repo |
| Dockerized solution | `Dockerfile` (python 3.12-slim, non-root, healthcheck, single worker), `docker-compose.yml` (alpha, beta, prism + named volume) |
| Detailed README | `README.md` (quick start, config, API, design decisions, limitations) |
| Tests | `tests/` — 58 tests, ~3 s, no network |
| CI | `.github/workflows/ci.yml` — pytest + routing eval, then builds the compose stack and runs the asserted demo drill against the containers |
| Demo video (< 5 min) | recorded by the author; follow `python3 scripts/demo.py --pause` |

## Good To Have (all attempted ones)

| Feature | Where | Test |
|---|---|---|
| Response-quality escalation (cheap-tier refusal → retry on `smart`, both calls billed) | `gateway`, `cache.is_refusal` | `test_escalation_when_cheap_model_refuses` |
| Provider health endpoint | `health.py`, `/admin/providers/health` | `test_routing_eval_endpoint_and_provider_health` |
| Tokens-per-minute limits | `limiter.py` (pre-charge estimate, corrected to real usage) | `test_token_amounts_and_adjust_refund` |
| Key-management APIs (create / disable / enable / rotate) | `app.py` | `test_key_management_create_disable_rotate` |
| Usage by model and by day | `store.usage_summary` | `test_accounting_is_exact_…` |

## Honest gaps

Stated again here so nothing is discovered late: the cache embedder is lexical rather than neural; the router's scores were tuned while looking at the provided set (a 30-case self-written held-out set is the only independent-ish check); the Docker image build was not run in the authoring environment, so the CI `docker` job is the authoritative check; budget overshoot is bounded by in-flight cost, not zero. Details in the README's *Known limitations*.

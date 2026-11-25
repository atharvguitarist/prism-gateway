"""Integration tests: the full pipeline against fake providers (no network, deterministic)."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx

from conftest import ADMIN, BUDGET, FREE, RESEARCH, SEARCH, chat


def logs(c, **q):
    return c.get("/admin/logs", params={"limit": 100, **q}, headers=ADMIN).json()["data"]


def usage(c, key):
    return c.get("/admin/usage", params={"key": key}, headers=ADMIN).json()


# ------------------------------------------------------------------ contract / errors
def test_header_contract_and_openai_shape(client):
    r = chat(client, SEARCH)
    assert r.status_code == 200
    assert r.headers["x-prism-provider"] == "alpha/alpha-small"
    assert r.headers["x-prism-cache"] == "miss" and r.headers["x-prism-fallback"] == "false"
    assert float(r.headers["x-prism-cost-usd"]) > 0
    b = r.json()
    assert b["object"] == "chat.completion" and b["choices"][0]["message"]["content"] and b["usage"]["total_tokens"]


def test_distinct_rejections(client):
    assert chat(client, "nope").status_code == 401
    assert client.post("/v1/chat/completions", json={}).status_code == 401
    r = chat(client, SEARCH, model="smart")
    assert (r.status_code, r.json()["error"]["type"]) == (403, "model_not_allowed")
    r = chat(client, SEARCH, model="no-such-model-xyz")
    assert (r.status_code, r.json()["error"]["type"]) == (404, "not_found_error")
    r = client.post("/v1/chat/completions", json={"model": "fast", "messages": []}, headers={"authorization": f"Bearer {SEARCH}"})
    assert (r.status_code, r.json()["error"]["type"]) == (400, "invalid_request_error")
    r = client.post("/v1/chat/completions", content=b"{not json", headers={"authorization": f"Bearer {SEARCH}"})
    assert r.status_code == 400


def test_every_request_including_rejections_is_logged(client):
    chat(client, SEARCH); chat(client, "bad-key"); chat(client, SEARCH, model="smart"); chat(client, SEARCH, model="zzz")
    statuses = [l["status"] for l in logs(client)]
    assert statuses == ["rejected_model", "rejected_allowlist", "rejected_auth", "ok"]  # newest first


def test_invalid_key_value_is_not_persisted(client):
    chat(client, "sk-live-someones-real-secret")
    assert all("someones-real-secret" not in json.dumps(l) for l in logs(client))


# ------------------------------------------------------------------ rate limit / budget
def test_rate_limit_is_enforced_and_logged(client):
    codes = [chat(client, FREE, f"q {i}").status_code for i in range(13)]
    assert codes.count(200) == 10 and codes.count(429) == 3
    r = chat(client, FREE, "one more")
    assert r.json()["error"]["type"] == "rate_limit_exceeded" and int(r.headers["retry-after"]) >= 1
    assert any(l["status"] == "rejected_rate_limit" for l in logs(client, key=FREE))


def test_budget_exhaustion_first_request_consumes_second_is_rejected(client):
    p = "What is a message queue and when should I use one?"   # realistic prompt: cost > the $0.00001 budget
    assert chat(client, BUDGET, p).status_code == 200
    r = chat(client, BUDGET, p + " again")
    assert (r.status_code, r.json()["error"]["type"]) == (402, "budget_exceeded")
    assert logs(client, key=BUDGET)[0]["status"] == "rejected_budget"      # the rejection itself is logged


def test_budget_survives_restart(tmp_path, providers):
    from starlette.testclient import TestClient
    from prism.app import create_app
    from conftest import make_cfg, make_settings
    s = make_settings(tmp_path)
    with TestClient(create_app(s, adapters=providers, cfg=make_cfg(s))) as c:
        assert chat(c, BUDGET, "What is a message queue and when should I use one?").status_code == 200
    with TestClient(create_app(s, adapters=providers, cfg=make_cfg(s))) as c2:   # fresh process state
        assert chat(c2, BUDGET, "another long enough question please").status_code == 402


# ------------------------------------------------------------------ failover / retries
def test_failover_when_primary_down_sets_header_and_log(client, providers):
    providers["alpha"].mode = "down"
    r = chat(client, SEARCH, "failover please")
    assert r.status_code == 200 and r.headers["x-prism-fallback"] == "true"
    assert r.headers["x-prism-provider"] == "beta/beta-small"
    row = logs(client)[0]
    assert row["fallback"] is True and row["retries"] == 3 and row["resolved_provider"] == "beta"
    assert providers["alpha"].calls == ["alpha-small"] * 3        # retried with backoff before failing over
    providers["alpha"].mode = "ok"
    assert chat(client, SEARCH, "restored now").headers["x-prism-provider"] == "alpha/alpha-small"  # traffic returns


def test_timeout_and_ratelimit_fail_over_immediately_without_retry(client, providers):
    for mode in ("timeout", "ratelimited"):
        providers["alpha"].mode, providers["alpha"].calls = mode, []
        r = chat(client, SEARCH, f"q {mode}")
        assert r.headers["x-prism-fallback"] == "true" and len(providers["alpha"].calls) == 1


def test_transient_errors_are_absorbed_by_retry(client, providers):
    providers["alpha"].fail_first_n = 2
    r = chat(client, SEARCH, "flaky")
    assert r.status_code == 200 and r.headers["x-prism-fallback"] == "false" and logs(client)[0]["retries"] == 2


def test_all_providers_down_is_a_clean_502(client, providers):
    providers["alpha"].mode = providers["beta"].mode = "down"
    r = chat(client, SEARCH, "everything is on fire")
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
    assert "mock-key" not in r.text and logs(client)[0]["status"] == "upstream_error"


def test_malformed_upstream_is_handled(client, providers):
    providers["alpha"].mode = "malformed"
    assert chat(client, SEARCH, "garbage upstream").status_code == 200   # beta answered


# ------------------------------------------------------------------ smart routing / escalation
def test_auto_routes_by_difficulty_and_logs_reason(client):
    hard = chat(client, RESEARCH, "Prove that the square root of 2 is irrational.", model="auto")
    easy = chat(client, RESEARCH, "What is the capital of France?", model="auto")
    assert hard.headers["x-prism-provider"] == "alpha/alpha-large" and hard.headers["x-prism-route"] == "auto:smart"
    assert easy.headers["x-prism-provider"] == "alpha/alpha-small" and easy.headers["x-prism-route"] == "auto:fast"
    rows = logs(client, routed=1)
    assert "proof/derivation" in rows[1]["route_reason"] and rows[1]["requested_model"] == "auto"


def test_auto_requires_allowlist(client):
    r = chat(client, SEARCH, "hi", model="auto")
    assert r.status_code == 403


def test_escalation_when_cheap_model_refuses(client):
    r = chat(client, RESEARCH, "please [refuse] this", model="fast")
    assert r.headers.get("x-prism-escalated") == "true" and r.headers["x-prism-provider"].endswith("large")
    row = logs(client)[0]
    assert row["escalated"] and "escalated fast->smart" in row["route_reason"]
    # key without 'smart' on its allowlist is never escalated into a model it may not use
    r2 = chat(client, SEARCH, "please [refuse] that")
    assert "x-prism-escalated" not in r2.headers


# ------------------------------------------------------------------ cache
def test_semantic_cache_hit_on_paraphrase_costs_zero(client):
    a = chat(client, SEARCH, "How do I reset my password on the dashboard?")
    b = chat(client, SEARCH, "What are the steps to reset my dashboard password?")
    c = chat(client, SEARCH, "How do I reset my two-factor authentication on the dashboard?")
    assert (a.headers["x-prism-cache"], b.headers["x-prism-cache"], c.headers["x-prism-cache"]) == ("miss", "hit", "miss")
    assert b.headers["x-prism-cost-usd"] == "0" and b.json()["usage"]["total_tokens"] == 0
    assert b.json()["choices"][0]["message"]["content"] == a.json()["choices"][0]["message"]["content"]
    assert usage(client, SEARCH)["cache_hits"] == 1


def test_cache_never_leaks_across_tenants(client):
    chat(client, SEARCH, "What is the refund policy for annual plans?")                      # warms search's cache
    r = chat(client, FREE, "What is the refund policy for annual plans?")                   # free-tier also caches
    assert r.headers["x-prism-cache"] == "miss"


def test_cache_disabled_key_and_time_sensitive_prompts_are_never_cached(client):
    for _ in range(2):
        assert chat(client, RESEARCH, "Explain consistent hashing").headers["x-prism-cache"] == "miss"
    for _ in range(2):
        assert chat(client, SEARCH, "What is the current status of the payments service?").headers["x-prism-cache"] == "miss"


def test_errors_are_not_cached(client, providers):
    providers["alpha"].mode = providers["beta"].mode = "down"
    chat(client, SEARCH, "unique question about widgets")
    providers["alpha"].mode = providers["beta"].mode = "ok"
    assert chat(client, SEARCH, "unique question about widgets").headers["x-prism-cache"] == "miss"


# ------------------------------------------------------------------ streaming
def _sse(resp_text):
    return [l[6:] for l in resp_text.splitlines() if l.startswith("data: ")]


def test_stream_shape_metering_and_headers(client):
    with client.stream("POST", "/v1/chat/completions", headers={"authorization": f"Bearer {SEARCH}"},
                       json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Explain SSE."}]}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-prism-provider"] == "alpha/alpha-small" and r.headers["x-prism-cache"] == "miss"
        assert "x-prism-cost-usd" not in r.headers          # cost unknown until the final usage chunk
        data = _sse("".join(r.iter_text()))
    assert data[-1] == "[DONE]" and len(data) >= 4
    row = logs(client)[0]
    assert row["status"] == "ok" and row["stream"] and row["cost_usd"] > 0 and row["completion_tokens"] > 0


def test_stream_populates_cache_and_hit_is_replayed_as_sse(client):
    h = {"authorization": f"Bearer {SEARCH}"}
    body = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Describe load balancers."}]}
    with client.stream("POST", "/v1/chat/completions", headers=h, json=body) as r:
        _ = "".join(r.iter_text())
    with client.stream("POST", "/v1/chat/completions", headers=h, json=body) as r:
        assert r.headers["x-prism-cache"] == "hit"
        data = _sse("".join(r.iter_text()))
    assert data[-1] == "[DONE]" and any('"content": "[alpha-small]' in d or "answer to" in d for d in data)


def test_stream_failover_before_first_byte(client, providers):
    providers["alpha"].mode = "down"
    with client.stream("POST", "/v1/chat/completions", headers={"authorization": f"Bearer {SEARCH}"},
                       json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "hi there"}]}) as r:
        assert r.headers["x-prism-fallback"] == "true" and r.headers["x-prism-provider"].startswith("beta/")
        assert _sse("".join(r.iter_text()))[-1] == "[DONE]"


def test_mid_stream_failure_emits_error_event_never_splices_providers(client, providers):
    providers["alpha"].stream_fail_after = 2
    with client.stream("POST", "/v1/chat/completions", headers={"authorization": f"Bearer {SEARCH}"},
                       json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "long answer please for me"}]}) as r:
        data = _sse("".join(r.iter_text()))
    assert "[DONE]" not in data                                   # a truncated answer must not look complete
    assert json.loads(data[-1])["error"]["code"] == "stream_interrupted"
    assert providers["beta"].calls == []                           # no silent restart on another provider
    row = logs(client)[0]
    assert row["status"] == "stream_error" and row["resolved_provider"] == "alpha"


def test_stream_without_usage_is_estimated_and_flagged(client, providers):
    providers["alpha"].omit_usage = True
    with client.stream("POST", "/v1/chat/completions", headers={"authorization": f"Bearer {SEARCH}"},
                       json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "no usage chunk"}]}) as r:
        "".join(r.iter_text())
    row = logs(client)[0]
    assert row["status"] == "ok" and row["completion_tokens"] > 0 and "estimated" in row["error"]


def test_streaming_is_passed_through_without_buffering(live_server, providers):
    """The provider emits chunk #1 then *blocks*. If the gateway buffered, the client could never
    see chunk #1 until the provider finished -- so receiving it first proves pass-through."""
    url, _ = live_server
    gate = providers["alpha"].gate = threading.Event()
    seen_first = threading.Event()
    got = []
    def reader():
        with httpx.stream("POST", f"{url}/v1/chat/completions", headers={"authorization": f"Bearer {SEARCH}"},
                          json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "unbuffered check"}]},
                          timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    got.append(line)
                    if len(got) == 1:
                        seen_first.set()
    t = threading.Thread(target=reader); t.start()
    assert seen_first.wait(5), "first chunk did not reach the client while the provider was still mid-stream"
    assert not gate.is_set() and t.is_alive()
    gate.set(); t.join(10)
    assert got[-1] == "data: [DONE]"


# ------------------------------------------------------------------ accounting under concurrency
def test_accounting_is_exact_and_limiter_race_free_under_concurrent_load(live_server):
    url, _ = live_server
    def one(i):
        r = httpx.post(f"{url}/v1/chat/completions", headers={"authorization": f"Bearer {FREE}"}, timeout=20,
                       json={"model": "fast", "messages": [{"role": "user", "content": f"concurrent load request {i}"}]})
        return r.status_code, r.headers.get("x-prism-cost-usd"), (r.json().get("usage") if r.status_code == 200 else None)
    with ThreadPoolExecutor(30) as ex:
        res = list(ex.map(one, range(60)))
    ok = [r for r in res if r[0] == 200]
    assert len(ok) == 10 and sum(1 for r in res if r[0] == 429) == 50            # zero over-admission
    u = httpx.get(f"{url}/admin/usage", params={"key": FREE}, headers=ADMIN).json()
    assert u["requests"] == 10 and u["rejected"] == 50
    assert u["prompt_tokens"] == sum(r[2]["prompt_tokens"] for r in ok)
    assert u["completion_tokens"] == sum(r[2]["completion_tokens"] for r in ok)
    assert Decimal(str(u["cost_usd"])) == sum(Decimal(r[1]) for r in ok)          # exact, to the nano-dollar


# ------------------------------------------------------------------ admin plane
def test_admin_requires_token_and_serves_console(client):
    assert client.get("/admin/usage").status_code == 401
    assert client.get("/admin/logs", headers={"x-admin-token": "wrong"}).status_code == 401
    assert client.get("/admin/logs", headers=ADMIN).status_code == 200
    assert "Prism" in client.get("/console").text


def test_key_management_create_disable_rotate(client):
    spec = {"team": "new team", "monthly_budget_usd": 1, "rate_limit": {"requests_per_minute": 5},
            "model_allowlist": ["fast"], "semantic_cache": {"enabled": True, "similarity_threshold": 0.9}}
    r = client.post("/admin/keys", json=spec, headers=ADMIN)
    assert r.status_code == 201
    key = r.json()["virtual_key"]
    assert chat(client, key).status_code == 200
    assert client.post(f"/admin/keys/{key}/disable", headers=ADMIN).status_code == 200
    assert chat(client, key).status_code == 401
    client.post(f"/admin/keys/{key}/enable", headers=ADMIN)
    new = client.post(f"/admin/keys/{key}/rotate", headers=ADMIN).json()["virtual_key"]
    assert chat(client, key).status_code == 401 and chat(client, new).status_code == 200
    assert client.post("/admin/keys", json={"team": "x"}, headers=ADMIN).status_code == 400


def test_routing_eval_endpoint_and_provider_health(client, providers):
    rep = client.get("/admin/routing/eval", headers=ADMIN).json()
    assert rep["total"] == 20 and rep["accuracy"] >= 0.85
    providers["alpha"].mode = "down"
    chat(client, SEARCH, "make alpha look sick")
    h = {p["provider"]: p for p in client.get("/admin/providers/health", headers=ADMIN).json()["data"]}
    assert h["alpha"]["status"] == "degraded" and h["beta"]["status"] == "healthy"


def test_provider_secrets_never_leak(client, providers):
    providers["alpha"].mode = providers["beta"].mode = "down"
    blob = chat(client, SEARCH, "leak check").text + json.dumps(logs(client)) + client.get("/admin/keys", headers=ADMIN).text
    assert "mock-key-alpha" not in blob and "mock-key-beta" not in blob

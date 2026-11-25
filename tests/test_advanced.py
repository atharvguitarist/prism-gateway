"""Circuit breaker in the pipeline, metrics, key updates, cache purge, config view."""
from conftest import ADMIN, FREE, RESEARCH, SEARCH, chat


def logs(c, **q):
    return c.get("/admin/logs", params={"limit": 100, **q}, headers=ADMIN).json()["data"]


# ------------------------------------------------------------------ circuit breaker in the pipeline
def test_dead_primary_is_skipped_after_the_breaker_opens(client, providers):
    providers["alpha"].mode = "down"
    first = chat(client, SEARCH, "first request pays the retries")
    assert first.status_code == 200 and providers["alpha"].calls == ["alpha-small"] * 3
    providers["alpha"].calls.clear()
    second = chat(client, SEARCH, "second request skips alpha entirely")
    assert second.headers["x-prism-provider"] == "beta/beta-small" and second.headers["x-prism-fallback"] == "true"
    assert second.headers["x-prism-circuit-skipped"] == "alpha"
    assert providers["alpha"].calls == []                            # not even attempted: no retry tax, no timeout tax
    row = logs(client)[0]
    assert row["retries"] == 0 and "circuit_skipped:alpha" in row["flags"]


def test_probe_recovers_the_primary(client, providers):
    gw = client.app_state.gw
    providers["alpha"].mode = "down"
    chat(client, SEARCH, "trip the breaker")
    providers["alpha"].mode = "ok"
    gw.breaker.cooldown = 0
    r = chat(client, SEARCH, "this one is the probe")
    assert r.headers["x-prism-provider"] == "alpha/alpha-small" and "x-prism-circuit-skipped" not in r.headers
    assert gw.breaker.state("alpha") == "closed"


def test_failed_probe_reopens_without_retrying_the_probe(client, providers):
    gw = client.app_state.gw
    providers["alpha"].mode = "down"
    chat(client, SEARCH, "trip")
    providers["alpha"].calls.clear()
    gw.breaker.cooldown = 0
    r = chat(client, SEARCH, "probe that fails")
    assert r.status_code == 200 and r.headers["x-prism-provider"] == "beta/beta-small"
    assert providers["alpha"].calls == ["alpha-small"]               # exactly one probe attempt, no backoff retries
    assert gw.breaker.snapshot(["alpha"])[0]["trips"] == 2


def test_all_circuits_open_fails_open_instead_of_502(client, providers):
    providers["alpha"].mode = providers["beta"].mode = "down"
    chat(client, SEARCH, "both down 1")                # opens both
    providers["beta"].mode = "ok"
    r = chat(client, SEARCH, "both circuits open, beta is actually back")
    assert r.status_code == 200 and r.headers["x-prism-provider"] == "beta/beta-small"


def test_provider_4xx_does_not_trip_the_breaker(client, providers):
    gw = client.app_state.gw
    from prism.providers import UpstreamError

    async def bad(model, payload, timeout_s):
        raise UpstreamError("http", "bad request for this provider", 400, retry_same=False)
    providers["alpha"].chat = bad
    for i in range(5):
        chat(client, SEARCH, f"q{i}")
    assert gw.breaker.state("alpha") == "closed"


def test_breaker_state_is_in_provider_health(client, providers):
    providers["alpha"].mode = "down"
    chat(client, SEARCH, "trip")
    d = {p["provider"]: p for p in client.get("/admin/providers/health", headers=ADMIN).json()["data"]}
    assert d["alpha"]["circuit"] == "open" and d["alpha"]["retry_in_s"] > 0 and d["beta"]["circuit"] == "closed"


# ------------------------------------------------------------------ budget header
def test_budget_remaining_header(client):
    r = chat(client, SEARCH)
    assert float(r.headers["x-prism-budget-remaining-usd"]) <= 50.0 and float(r.headers["x-prism-budget-remaining-usd"]) > 49.9


# ------------------------------------------------------------------ metrics
def test_metrics_requires_admin_and_is_prometheus_text(client, providers):
    assert client.get("/metrics").status_code == 401
    chat(client, SEARCH, "How do I reset my password on the dashboard?")
    chat(client, SEARCH, "What are the steps to reset my dashboard password?")      # paraphrase -> cache hit
    chat(client, "bad-key")
    r = client.get("/metrics", headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    assert '# TYPE prism_requests_total counter' in t
    assert 'prism_requests_total{status="ok"} 1' in t and 'prism_requests_total{status="cache_hit"} 1' in t and 'prism_requests_total{status="rejected_auth"} 1' in t
    assert 'prism_cache_hits_total 1' in t and 'prism_provider_circuit_open{provider="alpha"} 0' in t
    assert 'prism_key_budget_used_ratio{team="search"}' in t
    for line in t.splitlines():                                   # every sample line parses as `name{labels} number`
        if line and not line.startswith("#"):
            float(line.rsplit(" ", 1)[1])


# ------------------------------------------------------------------ key update
def test_patch_key_updates_limits_live(client):
    for i in range(10):
        assert chat(client, FREE, f"q {i}").status_code == 200
    assert chat(client, FREE, "q 11").status_code == 429
    r = client.patch(f"/admin/keys/{FREE}", json={"rate_limit": {"requests_per_minute": 100}, "monthly_budget_usd": 7.5,
                                                   "model_allowlist": ["fast", "smart"]}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["rate_limit"]["requests_per_minute"] == 100
    assert r.json()["monthly_budget_usd"] == 7.5 and r.json()["model_allowlist"] == ["fast", "smart"]
    assert chat(client, FREE, "q 12").status_code == 200           # new limit applies immediately
    assert chat(client, FREE, "now smart", model="smart").status_code == 200


def test_patch_key_validation(client):
    h = ADMIN
    assert client.patch(f"/admin/keys/{FREE}", json={"model_allowlist": ["nope"]}, headers=h).status_code == 400
    assert client.patch(f"/admin/keys/{FREE}", json={"monthly_budget_usd": -1}, headers=h).status_code == 400
    assert client.patch(f"/admin/keys/{FREE}", json={"bogus": 1}, headers=h).status_code == 400
    assert client.patch(f"/admin/keys/{FREE}", json={}, headers=h).status_code == 400
    assert client.patch("/admin/keys/missing", json={"team": "x"}, headers=h).status_code == 404
    assert client.patch(f"/admin/keys/{FREE}", json={"team": "x"}).status_code == 401


# ------------------------------------------------------------------ cache purge
def test_cache_purge_scoped_and_global(client):
    chat(client, SEARCH, "What is a message queue and when should I use one?")
    chat(client, FREE, "What is a message queue and when should I use one?")
    assert client.get("/admin/cache/stats", headers=ADMIN).json()["entries"] == 2
    r = client.delete("/admin/cache", params={"key": SEARCH}, headers=ADMIN)
    assert r.json() == {"purged": 1, "scope": SEARCH}
    assert chat(client, SEARCH, "What is a message queue and when should I use one?").headers["x-prism-cache"] == "miss"
    assert chat(client, FREE, "What is a message queue and when should I use one?").headers["x-prism-cache"] == "hit"
    assert client.delete("/admin/cache", headers=ADMIN).json()["scope"] == "all"
    assert client.get("/admin/cache/stats", headers=ADMIN).json()["entries"] == 0


# ------------------------------------------------------------------ config view
def test_config_view_never_exposes_provider_secrets(client):
    r = client.get("/admin/config", headers=ADMIN)
    assert r.status_code == 200 and "mock-key" not in r.text
    j = r.json()
    assert j["circuit_breaker"]["failure_threshold"] == 3 and j["guardrails"]["pii"] == "redact"
    assert {p["name"] for p in j["providers"]} == {"alpha", "beta"}

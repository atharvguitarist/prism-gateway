"""Prometheus text exposition (format 0.0.4), rendered on demand from the store and live gateway state.

Counters are derived from the request log for the current UTC month, so they reset at month rollover
(Prometheus handles counter resets). Gauges (circuit state, provider health, budget use) are live.
"""
from __future__ import annotations

from .pricing import format_usd


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(name: str, value, **labels) -> str:
    lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
    return f"{name}{{{lab}}} {value}" if lab else f"{name} {value}"


def render(gw, store) -> str:
    out: list[str] = []

    def head(name: str, typ: str, helptext: str) -> None:
        out.append(f"# HELP {name} {helptext}")
        out.append(f"# TYPE {name} {typ}")

    head("prism_requests_total", "counter", "Requests by outcome, current UTC month.")
    for status, n in sorted(store.status_counts().items()):
        out.append(_line("prism_requests_total", n, status=status))

    tot = store.usage_summary(None, None, None)
    head("prism_tokens_total", "counter", "Tokens metered from provider-reported usage, current UTC month.")
    out.append(_line("prism_tokens_total", tot["prompt_tokens"], type="prompt"))
    out.append(_line("prism_tokens_total", tot["completion_tokens"], type="completion"))
    head("prism_cost_usd_total", "counter", "Metered spend in USD, current UTC month.")
    out.append(_line("prism_cost_usd_total", tot["cost_usd"]))

    c = store.cache_stats()
    head("prism_cache_lookups_total", "counter", "Semantic cache lookups.")
    out.append(_line("prism_cache_lookups_total", c["lookups"]))
    head("prism_cache_hits_total", "counter", "Semantic cache hits (served at $0).")
    out.append(_line("prism_cache_hits_total", c["hits"]))
    head("prism_cache_savings_usd_total", "counter", "Spend avoided by cache hits.")
    out.append(_line("prism_cache_savings_usd_total", c["cost_saved_usd"]))
    head("prism_cache_entries", "gauge", "Entries currently stored in the semantic cache.")
    out.append(_line("prism_cache_entries", c["entries"]))

    head("prism_guardrail_events_total", "counter", "Guardrail and circuit-breaker events recorded on requests.")
    for ev, n in sorted(store.guardrail_counts().items()):
        kind, _, detail = ev.partition(":")
        out.append(_line("prism_guardrail_events_total", n, event=kind, detail=detail))

    names = list(gw.cfg.providers)
    head("prism_provider_circuit_open", "gauge", "1 when the provider's circuit is open (out of rotation).")
    head_c = gw.breaker.snapshot(names)
    for s in head_c:
        out.append(_line("prism_provider_circuit_open", 1 if s["circuit"] == "open" else 0, provider=s["provider"]))
    head("prism_provider_circuit_trips_total", "counter", "Times the provider's circuit has opened since start.")
    for s in head_c:
        out.append(_line("prism_provider_circuit_trips_total", s["trips"], provider=s["provider"]))
    head("prism_provider_error_rate", "gauge", "Upstream error rate over the last 60 s.")
    head("prism_provider_latency_p95_ms", "gauge", "Upstream p95 latency over the last 60 s.")
    for h in gw.health.snapshot(names):
        out.append(_line("prism_provider_error_rate", h["error_rate"], provider=h["provider"]))
        if h["p95_latency_ms"] is not None:
            out.append(_line("prism_provider_latency_p95_ms", h["p95_latency_ms"], provider=h["provider"]))

    oh = store.overhead_percentiles()
    head("prism_gateway_overhead_ms", "gauge", "Latency added by the gateway itself (excludes upstream time).")
    if oh.get("n"):
        out.append(_line("prism_gateway_overhead_ms", oh["p50_ms"], quantile="0.5"))
        out.append(_line("prism_gateway_overhead_ms", oh["p95_ms"], quantile="0.95"))

    head("prism_key_budget_used_ratio", "gauge", "Share of the monthly budget spent, per key.")
    head("prism_key_active", "gauge", "1 when the key is active.")
    for k in store.usage_by_key():
        out.append(_line("prism_key_budget_used_ratio", round((k.get("budget_used_pct") or 0) / 100, 6), team=k["team"]))
        out.append(_line("prism_key_active", 1 if k["status"] == "active" else 0, team=k["team"]))
    return "\n".join(out) + "\n"

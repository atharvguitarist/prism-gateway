"""Starlette application: data plane (`/v1/*`), admin plane (`/admin/*`), ops console (`/console`)."""
from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import GatewayConfig, Settings, load_gateway_config, load_pricing
from . import metrics as prom
from .gateway import Gateway, err_body
from .pricing import PriceTable, usd_to_nusd
from .providers import OpenAICompatAdapter, ProviderAdapter
from .routing_eval import run_eval
from .store import Store

CONSOLE = Path(__file__).parent / "console" / "index.html"

_PATCH_FIELDS = {"team", "monthly_budget_usd", "rate_limit", "model_allowlist", "semantic_cache"}


def parse_key_patch(spec, known_model) -> dict:
    """Validate a partial key update; raises ValueError with a client-safe message."""
    if not isinstance(spec, dict) or not spec:
        raise ValueError("body must be a non-empty JSON object")
    unknown = sorted(set(spec) - _PATCH_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)} (allowed: {', '.join(sorted(_PATCH_FIELDS))})")
    out: dict = {}
    if "team" in spec:
        if not isinstance(spec["team"], str) or not spec["team"].strip():
            raise ValueError("team must be a non-empty string")
        out["team"] = spec["team"].strip()
    if "monthly_budget_usd" in spec:
        b = spec["monthly_budget_usd"]
        if isinstance(b, bool) or not isinstance(b, (int, float)) or b < 0:
            raise ValueError("monthly_budget_usd must be a number >= 0")
        out["monthly_budget_usd"] = b
    if "rate_limit" in spec:
        rl = spec["rate_limit"]
        if not isinstance(rl, dict) or not rl or set(rl) - {"requests_per_minute", "tokens_per_minute"}:
            raise ValueError("rate_limit may contain requests_per_minute and/or tokens_per_minute")
        for k, v in rl.items():
            if v is None and k == "tokens_per_minute":
                continue   # null clears the token limit
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"rate_limit.{k} must be a positive integer")
        out["rate_limit"] = rl
    if "model_allowlist" in spec:
        a = spec["model_allowlist"]
        if not isinstance(a, list) or not a or not all(isinstance(x, str) for x in a):
            raise ValueError("model_allowlist must be a non-empty list of strings")
        bad = [x for x in a if not known_model(x)]
        if bad:
            raise ValueError(f"unknown model(s) in allowlist: {', '.join(bad)}")
        out["model_allowlist"] = a
    if "semantic_cache" in spec:
        sc = spec["semantic_cache"]
        if not isinstance(sc, dict) or not sc or set(sc) - {"enabled", "similarity_threshold"}:
            raise ValueError("semantic_cache may contain enabled and/or similarity_threshold")
        if "enabled" in sc and not isinstance(sc["enabled"], bool):
            raise ValueError("semantic_cache.enabled must be a boolean")
        if "similarity_threshold" in sc and (isinstance(sc["similarity_threshold"], bool)
                                             or not isinstance(sc["similarity_threshold"], (int, float))
                                             or not 0 < sc["similarity_threshold"] <= 1):
            raise ValueError("semantic_cache.similarity_threshold must be a number in (0, 1]")
        out["semantic_cache"] = sc
    return out


def create_app(settings: Optional[Settings] = None, *, adapters: Optional[dict[str, ProviderAdapter]] = None,
               cfg: Optional[GatewayConfig] = None) -> Starlette:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        gcfg = cfg or load_gateway_config(settings.config_path, settings)
        prices = PriceTable(load_pricing(settings.data_dir / "model_pricing.json"))
        store = Store(settings.db_path)
        with open(settings.data_dir / "seed_keys.json", encoding="utf-8") as f:
            store.seed_tenants(json.load(f)["tenants"], gcfg.cache.default_threshold)
        client = httpx.AsyncClient(limits=httpx.Limits(max_connections=500, max_keepalive_connections=100))
        ads = adapters or {n: OpenAICompatAdapter(n, p.base_url, p.api_key, client) for n, p in gcfg.providers.items()}
        gw = Gateway(gcfg, store, prices, ads)
        app.state.gw, app.state.store, app.state.settings, app.state.client = gw, store, settings, client
        try:
            yield
        finally:
            await gw.aclose()
            await client.aclose()
            store.close()

    # ---------------------------------------------------------------- admin auth
    def admin_only(handler):
        async def wrapped(request: Request) -> Response:
            tok = request.headers.get("x-admin-token") or ""
            if not tok:
                a = request.headers.get("authorization", "")
                tok = a[7:].strip() if a.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(tok.encode(), settings.admin_token.encode()):
                return JSONResponse(err_body("Admin token required (X-Admin-Token header)", "authentication_error"),
                                    status_code=401)
            return await handler(request)
        return wrapped

    # ---------------------------------------------------------------- data plane
    async def chat(request: Request) -> Response:
        return await request.app.state.gw.chat_completions(request)

    async def models(request: Request) -> Response:
        gw: Gateway = request.app.state.gw
        return JSONResponse({"object": "list", "data": [{"id": a, "object": "model", "owned_by": "prism"}
                                                        for a in gw.cfg.aliases]})

    async def health(request: Request) -> Response:
        gw: Gateway = request.app.state.gw
        out = {"status": "ok", "service": "prism"}
        if request.query_params.get("deep"):
            async def ping(name: str, base: str):
                root = base[:-3] if base.endswith("/v1") else base
                try:
                    r = await request.app.state.client.get(root + "/health", timeout=1.0)
                    return name, "up" if r.status_code == 200 else f"http_{r.status_code}"
                except httpx.HTTPError:
                    return name, "unreachable"
            out["providers"] = dict(await asyncio.gather(*[ping(n, p.base_url) for n, p in gw.cfg.providers.items()]))
        return JSONResponse(out)

    # ---------------------------------------------------------------- admin plane
    async def usage(request: Request) -> Response:
        st: Store = request.app.state.store
        q = request.query_params
        try:
            if q.get("key"):
                return JSONResponse(st.usage_summary(q["key"], q.get("from"), q.get("to")))
            return JSONResponse({"month": st.usage_by_key(), "all_keys": st.usage_summary(None, q.get("from"), q.get("to"))})
        except ValueError:
            return JSONResponse(err_body("from/to must be YYYY-MM-DD", "invalid_request_error"), status_code=400)

    async def logs(request: Request) -> Response:
        st: Store = request.app.state.store
        q = request.query_params
        try:
            limit = int(q.get("limit", "50"))
        except ValueError:
            limit = 50
        return JSONResponse({"data": st.recent_logs(q.get("key"), limit, q.get("status"), q.get("routed") == "1")})

    async def cache_stats(request: Request) -> Response:
        st: Store = request.app.state.store
        return JSONResponse(st.cache_stats(request.query_params.get("key")))

    async def providers_health(request: Request) -> Response:
        gw: Gateway = request.app.state.gw
        names = list(gw.cfg.providers)
        circuits = {c["provider"]: c for c in gw.breaker.snapshot(names)}
        data = [{**h, **{k: v for k, v in circuits[h["provider"]].items() if k != "provider"}}
                for h in gw.health.snapshot(names)]
        return JSONResponse({"data": data,
                             "gateway_overhead_ms": request.app.state.store.overhead_percentiles()})

    async def purge_cache(request: Request) -> Response:
        key = request.query_params.get("key") or None
        n = request.app.state.gw.cache.purge(key)
        return JSONResponse({"purged": n, "scope": key or "all"})

    async def metrics(request: Request) -> Response:
        text = prom.render(request.app.state.gw, request.app.state.store)
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    async def config_view(request: Request) -> Response:
        """The effective gateway config. Provider API keys are never included."""
        gw: Gateway = request.app.state.gw
        c = gw.cfg
        return JSONResponse({
            "providers": [{"name": p.name, "base_url": p.base_url, "models": p.models} for p in c.providers.values()],
            "aliases": {n: {"primary": a.primary, "fallbacks": a.fallbacks, "route_by_difficulty": a.route_by_difficulty}
                        for n, a in c.aliases.items()},
            "retry": {"max_attempts": c.retry.max_attempts, "initial_backoff_ms": c.retry.initial_backoff_ms,
                      "backoff_multiplier": c.retry.backoff_multiplier, "max_total_ms": c.retry.max_total_ms},
            "timeouts": {"upstream_ms": round(c.upstream_timeout_s * 1000),
                         "stream_idle_ms": round(c.stream_idle_timeout_s * 1000)},
            "cache": {"ttl_seconds": c.cache.ttl_seconds, "max_entries_per_key": c.cache.max_entries_per_key,
                      "default_threshold": c.cache.default_threshold},
            "escalation": {"enabled": c.escalation_enabled},
            "circuit_breaker": {"enabled": gw.breaker.enabled, "failure_threshold": gw.breaker.threshold,
                                "cooldown_s": gw.breaker.cooldown},
            "guardrails": {"pii": c.guardrails.pii, "injection": c.guardrails.injection},
        })

    async def routing_eval(request: Request) -> Response:
        data_dir: Path = request.app.state.settings.data_dir
        name = Path(request.query_params.get("file", "routing_eval.jsonl")).name  # no path traversal
        p = data_dir / name
        if not p.exists():
            return JSONResponse(err_body(f"no such eval file '{name}'", "not_found_error"), status_code=404)
        return JSONResponse(run_eval(p))

    async def list_keys(request: Request) -> Response:
        return JSONResponse({"data": [t.public() for t in request.app.state.store.list_tenants()]})

    async def create_key(request: Request) -> Response:
        gw: Gateway = request.app.state.gw
        try:
            spec = await request.json()
            assert isinstance(spec, dict) and isinstance(spec.get("team"), str) and spec["team"].strip()
            budget = float(spec["monthly_budget_usd"])
            rl = spec.get("rate_limit") or {}
            rpm = int(rl["requests_per_minute"])
            allow = spec["model_allowlist"]
            assert isinstance(allow, list) and allow and all(isinstance(a, str) for a in allow)
            assert all(a in gw.cfg.aliases or a in gw.prices for a in allow), "unknown model in allowlist"
            assert budget >= 0 and rpm > 0
        except (ValueError, KeyError, TypeError, AssertionError, json.JSONDecodeError) as e:
            return JSONResponse(err_body(f"Invalid key spec: {e or 'team, monthly_budget_usd, rate_limit.requests_per_minute, model_allowlist required'}",
                                         "invalid_request_error"), status_code=400)
        t = request.app.state.store.create_tenant(spec, gw.cfg.cache.default_threshold)
        return JSONResponse(t.public(), status_code=201)

    async def patch_key(request: Request) -> Response:
        gw: Gateway = request.app.state.gw
        try:
            changes = parse_key_patch(await request.json(), lambda m: m in gw.cfg.aliases or m in gw.prices)
        except json.JSONDecodeError:
            return JSONResponse(err_body("Invalid key update: body is not valid JSON", "invalid_request_error"),
                                status_code=400)
        except ValueError as e:
            return JSONResponse(err_body(f"Invalid key update: {e}", "invalid_request_error"), status_code=400)
        t = request.app.state.store.update_tenant(request.path_params["key"], changes)
        if t is None:
            return JSONResponse(err_body("no such key", "not_found_error"), status_code=404)
        return JSONResponse(t.public())

    async def disable_key(request: Request) -> Response:
        ok = request.app.state.store.set_status(request.path_params["key"], "disabled")
        return JSONResponse({"disabled": True} if ok else err_body("no such key", "not_found_error"),
                            status_code=200 if ok else 404)

    async def enable_key(request: Request) -> Response:
        ok = request.app.state.store.set_status(request.path_params["key"], "active")
        return JSONResponse({"enabled": True} if ok else err_body("no such key", "not_found_error"),
                            status_code=200 if ok else 404)

    async def rotate_key(request: Request) -> Response:
        new = request.app.state.store.rotate_key(request.path_params["key"])
        if not new:
            return JSONResponse(err_body("no such key", "not_found_error"), status_code=404)
        request.app.state.gw.cache.reload()
        return JSONResponse({"virtual_key": new, "note": "the previous key value is invalid immediately"})

    async def console(request: Request) -> Response:
        return FileResponse(CONSOLE, media_type="text/html")

    async def root(request: Request) -> Response:
        return RedirectResponse("/console")

    routes = [
        Route("/", root),
        Route("/health", health),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/models", models),
        Route("/console", console),
        Route("/admin/usage", admin_only(usage)),
        Route("/admin/logs", admin_only(logs)),
        Route("/admin/cache/stats", admin_only(cache_stats)),
        Route("/admin/cache", admin_only(purge_cache), methods=["DELETE"]),
        Route("/admin/config", admin_only(config_view)),
        Route("/metrics", admin_only(metrics)),
        Route("/admin/providers/health", admin_only(providers_health)),
        Route("/admin/routing/eval", admin_only(routing_eval)),
        Route("/admin/keys", admin_only(list_keys), methods=["GET"]),
        Route("/admin/keys", admin_only(create_key), methods=["POST"]),
        Route("/admin/keys/{key}", admin_only(patch_key), methods=["PATCH"]),
        Route("/admin/keys/{key}/disable", admin_only(disable_key), methods=["POST"]),
        Route("/admin/keys/{key}/enable", admin_only(enable_key), methods=["POST"]),
        Route("/admin/keys/{key}/rotate", admin_only(rotate_key), methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def app() -> Starlette:  # `uvicorn prism.app:app --factory`
    return create_app()

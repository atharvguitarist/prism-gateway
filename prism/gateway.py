"""The Prism data plane: one OpenAI-compatible `/v1/chat/completions` pipeline.

Request pipeline (the order matters and is documented in the README):

    1. authenticate virtual key            -> 401 authentication_error
    2. validate body                       -> 400 invalid_request_error
    3. resolve alias / model               -> 404 not_found_error
    4. model allowlist                     -> 403 model_not_allowed
    5. monthly budget (cheap PK read)      -> 402 budget_exceeded
    6. rate limit (atomic sliding window)  -> 429 rate_limit_exceeded
    7. `auto`: classify difficulty -> tier (logged with its reason)
    8. semantic cache lookup (per key)     -> hit: served at $0
    9. upstream call: timeout, retry w/ exponential backoff, ordered provider failover
   10. meter from provider-reported usage, write request log + usage atomically, respond

Every request -- including rejected ones -- produces exactly one request-log row.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .cache import SemanticCache, is_cacheable_request, is_refusal, last_user_text
from .config import GatewayConfig
from .health import ProviderHealth
from .limiter import SlidingWindowLimiter
from .pricing import PriceTable, UnknownModelPrice, format_usd
from .providers import ProviderAdapter, UpstreamError, parse_chunk
from .smart_router import classify
from .store import LogEntry, Store, Tenant

MAX_BODY_BYTES = 2 * 1024 * 1024


class AllProvidersFailed(Exception):
    def __init__(self, last: Optional[UpstreamError], failed_attempts: int, scope: str):
        super().__init__(str(last) if last else "no provider available")
        self.last, self.failed_attempts, self.scope = last, failed_attempts, scope


@dataclass
class Served:
    model: str
    provider: str
    result: Any            # ChatResult (non-stream) or StreamHandle (stream)
    fallback: bool
    failed_attempts: int
    upstream_ms: int


def _hdr(s: str, limit: int = 240) -> str:
    return s.encode("latin-1", "replace").decode("latin-1").replace("\n", " ")[:limit]


def err_body(message: str, etype: str, code: Optional[str] = None) -> dict:
    return {"error": {"message": message, "type": etype, "code": code or etype}}


class Gateway:
    def __init__(self, cfg: GatewayConfig, store: Store, prices: PriceTable,
                 adapters: dict[str, ProviderAdapter], limiter: Optional[SlidingWindowLimiter] = None,
                 cache: Optional[SemanticCache] = None, sleep=asyncio.sleep):
        self.cfg, self.store, self.prices, self.adapters = cfg, store, prices, adapters
        self.limiter = limiter or SlidingWindowLimiter()
        self.cache = cache or SemanticCache(store, ttl_seconds=cfg.cache.ttl_seconds,
                                            max_entries_per_key=cfg.cache.max_entries_per_key)
        self._sleep = sleep
        self.health = ProviderHealth(window_s=60.0)
        self._bg: set[asyncio.Task] = set()
        self._validate_config()

    # ------------------------------------------------------------ startup validation
    def _validate_config(self) -> None:
        for alias in self.cfg.aliases.values():
            if alias.route_by_difficulty:
                for tier, target in alias.route_by_difficulty.items():
                    if target not in self.cfg.aliases or not self.cfg.aliases[target].chain:
                        raise ValueError(f"alias '{alias.alias}' tier '{tier}' -> unknown alias '{target}'")
                continue
            for m in alias.chain:
                if m not in self.prices:
                    raise ValueError(f"alias '{alias.alias}' uses model '{m}' which is not in the price table")
                p = self.cfg.provider_for_model(m)
                if p is None or p.name not in self.adapters:
                    raise ValueError(f"alias '{alias.alias}' uses model '{m}' with no registered provider")

    # ------------------------------------------------------------------ helpers
    def _provider_of(self, model: str) -> ProviderAdapter:
        return self.adapters[self.cfg.provider_for_model(model).name]  # type: ignore[union-attr]

    def _resolve(self, requested: str) -> Optional[tuple[str, list[str], bool]]:
        """-> (scope, chain, is_auto) or None when the alias/model is unknown."""
        a = self.cfg.aliases.get(requested)
        if a is not None:
            if a.route_by_difficulty:
                return requested, [], True
            return requested, a.chain, False
        if requested in self.prices and self.cfg.provider_for_model(requested):
            return requested, [requested], False   # direct model: pinned, no fallbacks
        return None

    def _contract_headers(self, rid: str, provider: str = "none", cache: str = "miss", fallback: bool = False,
                          cost: Optional[int] = 0, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {"x-prism-request-id": rid, "x-prism-provider": provider, "x-prism-cache": cache,
             "x-prism-fallback": "true" if fallback else "false"}
        if cost is not None:
            h["x-prism-cost-usd"] = format_usd(cost)
        if extra:
            h.update(extra)
        return h

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------------ upstream
    async def _run_chain(self, chain: list[str], scope: str, call) -> Served:
        """Try each model in `chain` in order; retry transient errors with exponential backoff;
        fail over on down / rate-limited / timed-out providers. Every attempt has a timeout."""
        rc = self.cfg.retry
        start, failed, last = time.perf_counter(), 0, None
        for idx, model in enumerate(chain):
            adapter = self._provider_of(model)
            for attempt in range(rc.max_attempts):
                if failed and (time.perf_counter() - start) * 1000 > rc.max_total_ms:
                    raise AllProvidersFailed(last, failed, scope)
                t0 = time.perf_counter()
                try:
                    res = await call(adapter, model)
                    ms = int((time.perf_counter() - t0) * 1000)
                    self.health.record(adapter.name, True, ms)
                    return Served(model, adapter.name, res, idx > 0, failed, ms)
                except UpstreamError as e:
                    self.health.record(adapter.name, False, int((time.perf_counter() - t0) * 1000), e.kind)
                    last, failed = e, failed + 1
                    if e.retry_same and attempt + 1 < rc.max_attempts:
                        await self._sleep(rc.initial_backoff_ms / 1000 * (rc.backoff_multiplier ** attempt))
                        continue
                    break  # move on to the next provider in the chain
        raise AllProvidersFailed(last, failed, scope)

    async def _chat_chain(self, chain, scope, payload) -> Served:
        p = {**payload, "stream": False}
        return await self._run_chain(chain, scope, lambda ad, m: ad.chat(m, p, self.cfg.upstream_timeout_s))

    async def _stream_chain(self, chain, scope, payload) -> Served:
        return await self._run_chain(
            chain, scope, lambda ad, m: ad.open_stream(m, payload, self.cfg.upstream_timeout_s,
                                                        self.cfg.stream_idle_timeout_s))

    # ------------------------------------------------------------------ main entry
    async def chat_completions(self, request: Request) -> Response:
        t0 = time.perf_counter()
        rid = "req_" + uuid.uuid4().hex[:16]
        log = LogEntry(request_id=rid, virtual_key=None, requested_model=None, status="", http_status=0)

        def finish(status: int, log_status: str, body: dict, headers: Optional[dict] = None,
                   hdr_extra: Optional[dict] = None) -> Response:
            log.status, log.http_status = log_status, status
            log.latency_ms = int((time.perf_counter() - t0) * 1000)
            log.overhead_ms = log.latency_ms
            self.store.record_request(log)
            h = self._contract_headers(rid, extra=hdr_extra)
            h.update(headers or {})
            return JSONResponse(body, status_code=status, headers=h)

        # 1 --- authentication
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        tenant = self.store.get_tenant(token) if token else None
        if tenant is None:
            return finish(401, "rejected_auth", err_body("Invalid or missing virtual key", "authentication_error"))
        log.virtual_key = tenant.virtual_key
        if not tenant.active:
            return finish(401, "rejected_auth", err_body("This virtual key is disabled", "authentication_error",
                                                          "key_disabled"))

        # 2 --- body validation
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return finish(413, "rejected_invalid", err_body("Request body too large", "invalid_request_error",
                                                             "body_too_large"))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return finish(400, "rejected_invalid", err_body("Request body is not valid JSON", "invalid_request_error"))
        problem = self._validate_body(body)
        if problem:
            log.requested_model = body.get("model") if isinstance(body, dict) and isinstance(body.get("model"), str) else None
            return finish(400, "rejected_invalid", err_body(problem, "invalid_request_error"))
        requested, messages, stream = body["model"], body["messages"], bool(body.get("stream"))
        log.requested_model, log.stream = requested, stream

        # 3 --- alias / model resolution
        resolved = self._resolve(requested)
        if resolved is None:
            known = ", ".join(sorted(self.cfg.aliases))
            return finish(404, "rejected_model",
                          err_body(f"Unknown model or alias '{requested}'. Available aliases: {known}",
                                   "not_found_error", "model_not_found"))
        scope, chain, is_auto = resolved

        # 4 --- allowlist
        if requested not in tenant.allowlist:
            return finish(403, "rejected_allowlist",
                          err_body(f"Model '{requested}' is not allowed for this key (allowed: "
                                   f"{', '.join(tenant.allowlist)})", "model_not_allowed"))

        # 5 --- budget (read-only here; money is added atomically when the request completes)
        spent = self.store.month_spend_nusd(tenant.virtual_key)
        if spent >= tenant.monthly_budget_nusd:
            return finish(402, "rejected_budget",
                          err_body(f"Monthly budget of ${format_usd(tenant.monthly_budget_nusd)} exhausted for this key "
                                   f"(spent ${format_usd(spent)})", "budget_exceeded"))

        # 6 --- rate limits: atomic check-and-consume
        prompt_text = last_user_text(messages)
        est_tokens = max(1, sum(len(str(m.get("content", "")).split()) for m in messages))
        rl = self.limiter.try_acquire(f"rpm:{tenant.virtual_key}", tenant.rpm, 1)
        if not rl.allowed:
            return self._rate_limited(finish, rl.retry_after_s, f"{tenant.rpm} requests/minute")
        tpm_key = f"tpm:{tenant.virtual_key}"
        if tenant.tpm:
            tl = self.limiter.try_acquire(tpm_key, tenant.tpm, est_tokens)
            if not tl.allowed:
                self.limiter.adjust(f"rpm:{tenant.virtual_key}", -1)  # rejected: give the request slot back
                return self._rate_limited(finish, tl.retry_after_s, f"{tenant.tpm} tokens/minute")

        # 7 --- smart routing
        extra_hdr: dict[str, str] = {}
        if is_auto:
            decision = classify(prompt_text)
            tier, reason = decision.tier, decision.short_reason()
            allowed_tiers = [t for t in ("fast", "smart") if t in tenant.allowlist]
            if tier not in allowed_tiers:
                if not allowed_tiers:
                    self.limiter.adjust(f"rpm:{tenant.virtual_key}", -1)
                    return finish(403, "rejected_allowlist",
                                  err_body("Key allows 'auto' but neither 'fast' nor 'smart'", "model_not_allowed"))
                reason += f" | downgraded to '{allowed_tiers[0]}': key allowlist excludes '{tier}'"
                tier = allowed_tiers[0]
            scope, chain = tier, self.cfg.aliases[tier].chain
            log.route_reason = reason
            extra_hdr = {"x-prism-route": f"auto:{tier}", "x-prism-route-reason": _hdr(reason)}

        payload = {k: v for k, v in body.items()}
        t_pre = time.perf_counter()

        # 8 --- semantic cache
        cacheable, _why = is_cacheable_request(body, prompt_text)
        use_cache = bool(tenant.cache_enabled and cacheable)
        log.cache_lookup = use_cache
        if use_cache:
            thr = tenant.cache_threshold or self.cfg.cache.default_threshold
            hit = self.cache.lookup(tenant.virtual_key, scope, messages, thr)
            if hit:
                return self._serve_hit(hit, stream, log, rid, t0, tenant, est_tokens, tpm_key, extra_hdr)

        # 9 --- upstream
        if stream:
            return await self._serve_stream(payload, chain, scope, log, rid, t0, t_pre, tenant, messages,
                                            use_cache, est_tokens, tpm_key, extra_hdr, finish)
        return await self._serve_nonstream(payload, chain, scope, log, rid, t0, t_pre, tenant, messages,
                                           use_cache, est_tokens, tpm_key, extra_hdr, finish)

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _validate_body(body: Any) -> Optional[str]:
        if not isinstance(body, dict):
            return "Request body must be a JSON object"
        if not isinstance(body.get("model"), str) or not body["model"].strip():
            return "'model' is required and must be a string"
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return "'messages' must be a non-empty array"
        for i, m in enumerate(msgs):
            if not isinstance(m, dict) or not isinstance(m.get("role"), str):
                return f"messages[{i}] must be an object with a string 'role'"
            if not isinstance(m.get("content", ""), (str, list, type(None))):
                return f"messages[{i}].content must be a string or content-part array"
        if "stream" in body and not isinstance(body["stream"], bool):
            return "'stream' must be a boolean"
        if not any(m.get("role") == "user" for m in msgs):
            return "'messages' must contain at least one message with role 'user'"
        return None

    def _rate_limited(self, finish, retry_after_s: float, what: str) -> Response:
        ra = max(1, math.ceil(retry_after_s))
        return finish(429, "rejected_rate_limit",
                      err_body(f"Rate limit exceeded ({what}). Retry in {ra}s", "rate_limit_exceeded"),
                      headers={"retry-after": str(ra)})

    def _serve_hit(self, hit, stream, log: LogEntry, rid, t0, tenant, est_tokens, tpm_key, extra_hdr) -> Response:
        e = hit.entry
        self.limiter.adjust(tpm_key, -est_tokens) if tenant.tpm else None  # a hit consumes no tokens
        log.status, log.http_status, log.cache = "cache_hit", 200, "hit"
        log.saved_nusd = e.cost_nusd
        log.latency_ms = int((time.perf_counter() - t0) * 1000)
        log.overhead_ms = log.latency_ms
        self.store.record_request(log)
        content, model = e.response["content"], e.response.get("model", "cached")
        h = self._contract_headers(rid, provider=e.origin, cache="hit", cost=0,
                                   extra={**extra_hdr, "x-prism-cache-similarity": f"{hit.similarity:.3f}"})
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
        zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not stream:
            return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                              "finish_reason": "stop"}], "usage": zero}, headers=h)

        async def replay() -> AsyncIterator[bytes]:  # cached answers are replayed as one content chunk
            def ch(delta, fin=None, usage=None):
                d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                if usage is not None:
                    d["usage"] = usage
                return f"data: {json.dumps(d)}\n\n".encode()
            yield ch({"role": "assistant", "content": ""})
            yield ch({"content": content})
            yield ch({}, "stop", zero)
            yield b"data: [DONE]\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream",
                                 headers={**h, "cache-control": "no-cache", "x-accel-buffering": "no"})

    def _cost(self, model: str, pt: int, ct: int) -> int:
        try:
            return self.prices.cost_nusd(model, pt, ct)
        except UnknownModelPrice:
            return 0

    async def _serve_nonstream(self, payload, chain, scope, log: LogEntry, rid, t0, t_pre, tenant, messages,
                               use_cache, est_tokens, tpm_key, extra_hdr, finish) -> Response:
        try:
            served = await self._chat_chain(chain, scope, payload)
        except AllProvidersFailed as e:
            return self._upstream_failed(e, finish, log, t0, t_pre, tpm_key, tenant, est_tokens)
        body = served.result.body
        upstream_ms = served.upstream_ms
        usage = body["usage"]
        pt, ct = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        cost = self._cost(served.model, pt, ct)
        final_provider, final_model, fallback, retries = served.provider, served.model, served.fallback, served.failed_attempts
        content = body["choices"][0]["message"]["content"]
        escalated = False

        # Good-to-have: response-quality escalation. If the cheap tier *refused*, retry once on the strong tier.
        if (self.cfg.escalation_enabled and scope == "fast" and "smart" in tenant.allowlist
                and is_refusal(content) and "smart" in self.cfg.aliases):
            try:
                s2 = await self._chat_chain(self.cfg.aliases["smart"].chain, "smart", payload)
            except AllProvidersFailed:
                s2 = None
            if s2 is not None:
                b2 = s2.result.body
                pt2, ct2 = int(b2["usage"]["prompt_tokens"]), int(b2["usage"]["completion_tokens"])
                cost += self._cost(s2.model, pt2, ct2)   # the refused call was paid for too
                pt, ct = pt + pt2, ct + ct2
                body, content, escalated = b2, b2["choices"][0]["message"]["content"], True
                final_provider, final_model, fallback = s2.provider, s2.model, s2.fallback
                retries += s2.failed_attempts
                upstream_ms += s2.upstream_ms
                log.route_reason = ((log.route_reason + " | ") if log.route_reason else "") + \
                    "escalated fast->smart: cheap model refused"

        if tenant.tpm:
            self.limiter.adjust(tpm_key, pt + ct - est_tokens)
        log.status, log.http_status = "ok", 200
        log.resolved_provider, log.resolved_model = final_provider, final_model
        log.prompt_tokens, log.completion_tokens, log.cost_nusd = pt, ct, cost
        log.fallback, log.escalated, log.retries = fallback, escalated, retries
        log.cache, log.upstream_ms = "miss", upstream_ms
        log.latency_ms = int((time.perf_counter() - t0) * 1000)
        log.overhead_ms = max(0, log.latency_ms - upstream_ms)

        if use_cache and not is_refusal(content) and body["choices"][0].get("finish_reason") == "stop":
            self.cache.put(tenant.virtual_key, scope, messages,
                           {"model": final_model, "content": content, "usage": body["usage"]},
                           f"{final_provider}/{final_model}", cost)
        self.store.record_request(log)
        h = self._contract_headers(rid, provider=f"{final_provider}/{final_model}", cache="miss",
                                   fallback=fallback, cost=cost,
                                   extra={**extra_hdr, **({"x-prism-escalated": "true"} if escalated else {})})
        return JSONResponse(body, headers=h)

    def _upstream_failed(self, e: AllProvidersFailed, finish, log: LogEntry, t0, t_pre, tpm_key, tenant, est_tokens):
        if tenant.tpm:
            self.limiter.adjust(tpm_key, -est_tokens)
        log.retries, log.error = e.failed_attempts, str(e)[:300]
        log.upstream_ms = int((time.perf_counter() - t_pre) * 1000)
        kind = e.last.kind if e.last else "none"
        return finish(502, "upstream_error",
                      err_body(f"All providers failed for '{e.scope}' after {e.failed_attempts} attempt(s): {e}",
                               "upstream_error", f"upstream_{kind}"))

    # ------------------------------------------------------------------ streaming
    async def _serve_stream(self, payload, chain, scope, log: LogEntry, rid, t0, t_pre, tenant, messages,
                            use_cache, est_tokens, tpm_key, extra_hdr, finish) -> Response:
        try:
            served = await self._stream_chain(chain, scope, payload)
        except AllProvidersFailed as e:
            return self._upstream_failed(e, finish, log, t0, t_pre, tpm_key, tenant, est_tokens)
        handle = served.result
        provider_hdr = f"{served.provider}/{served.model}"
        log.resolved_provider, log.resolved_model = served.provider, served.model
        log.fallback, log.retries = served.fallback, served.failed_attempts
        log.overhead_ms = int((t_pre - t0) * 1000)
        t_up = time.perf_counter()
        h = self._contract_headers(rid, provider=provider_hdr, cache="miss", fallback=served.fallback,
                                   cost=None, extra=extra_hdr)   # cost unknown until the final usage chunk
        h.update({"cache-control": "no-cache", "x-accel-buffering": "no"})

        async def body() -> AsyncIterator[bytes]:
            parts: list[str] = []
            usage: Optional[dict] = None
            finish_reason: Optional[str] = None
            outcome, err = "ok", None
            saw_done = False
            try:
                async def all_payloads():
                    yield handle.first
                    async for d in handle.lines():
                        yield d
                async for data in all_payloads():
                    if data.strip() == "[DONE]":
                        saw_done = True
                        yield b"data: [DONE]\n\n"
                        break
                    obj = parse_chunk(data)
                    if obj is not None:
                        if isinstance(obj.get("usage"), dict):
                            usage = obj["usage"]
                        for c in obj.get("choices") or []:
                            piece = (c.get("delta") or {}).get("content")
                            if piece:
                                parts.append(piece)
                            if c.get("finish_reason"):
                                finish_reason = c["finish_reason"]
                    yield f"data: {data}\n\n".encode()   # forwarded as it arrives: no buffering
                if not saw_done:
                    raise UpstreamError("malformed", "upstream stream ended without [DONE]")
            except (UpstreamError, httpx.HTTPError, asyncio.TimeoutError) as ex:
                # Mid-stream failure policy: tell the client explicitly and stop. We never restart on another
                # provider and splice outputs -- the client already holds a partial answer from this one.
                outcome, err = "stream_error", f"{type(ex).__name__}: {ex}"[:300]
                msg = {"error": {"message": f"Upstream provider '{served.provider}' failed mid-stream; "
                                            f"the response is incomplete", "type": "upstream_error",
                                 "code": "stream_interrupted"}}
                yield f"data: {json.dumps(msg)}\n\n".encode()
            except (asyncio.CancelledError, GeneratorExit):
                outcome, err = "stream_aborted", "client disconnected mid-stream"
                raise
            finally:
                self._finish_stream(log, outcome, err, usage, parts, finish_reason, served, t0, t_up, tenant,
                                    messages, use_cache, est_tokens, tpm_key, scope, saw_done)
                self._spawn(handle.aclose())

        return StreamingResponse(body(), media_type="text/event-stream", headers=h)

    def _finish_stream(self, log: LogEntry, outcome, err, usage, parts, finish_reason, served: Served, t0, t_up,
                       tenant: Tenant, messages, use_cache, est_tokens, tpm_key, scope, saw_done) -> None:
        pt = ct = 0
        if usage:
            pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        elif outcome == "ok":   # finished cleanly but provider omitted usage: estimate, and say so
            pt, ct = est_tokens, max(1, len("".join(parts).split()))
            err = "usage missing from provider stream; tokens estimated from word count"
        cost = self._cost(served.model, pt, ct)
        if tenant.tpm:
            self.limiter.adjust(tpm_key, pt + ct - est_tokens)
        now = time.perf_counter()
        log.status, log.http_status = outcome, 200
        log.prompt_tokens, log.completion_tokens, log.cost_nusd = pt, ct, cost
        log.error, log.cache = err, "miss"
        log.upstream_ms = int((now - t_up) * 1000)
        log.latency_ms = int((now - t0) * 1000)
        text = "".join(parts)
        try:
            if outcome == "ok" and use_cache and text and not is_refusal(text) and finish_reason == "stop":
                self.cache.put(tenant.virtual_key, scope, messages,
                               {"model": served.model, "content": text, "usage": usage or {}},
                               f"{served.provider}/{served.model}", cost)
        finally:
            self.store.record_request(log)

    async def aclose(self) -> None:
        for t in list(self._bg):
            t.cancel()

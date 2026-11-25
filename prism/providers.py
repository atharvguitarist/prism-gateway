"""Provider adapter layer.

The gateway core only ever talks to the `ProviderAdapter` interface, so a mock, a
real hosted provider, or an in-process fake (used by the test-suite) are interchangeable.
`OpenAICompatAdapter` speaks the OpenAI chat-completions wire format over HTTP,
which is what both mock providers (and OpenAI, Groq, Ollama, vLLM ...) expose.

Every upstream call has a timeout, and every failure is normalised into `UpstreamError`
with an explicit policy flag (`retry_same` / `failover`) so the router never has to
guess from provider-specific exception types.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Protocol

import httpx


class UpstreamError(Exception):
    """A failed upstream call, already classified for the retry / failover policy."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None,
                 retry_same: bool = False, failover: bool = True):
        super().__init__(message)
        self.kind = kind            # timeout | connect | http | malformed | rate_limited
        self.status = status
        self.retry_same = retry_same  # worth retrying the same provider (with backoff)?
        self.failover = failover      # worth moving on to the next provider?


@dataclass
class ChatResult:
    body: dict[str, Any]
    status: int = 200


class StreamHandle(Protocol):
    first: Optional[str]                      # first SSE `data:` payload, already read
    def lines(self) -> AsyncIterator[str]: ...  # remaining `data:` payloads (raw text after "data:")
    async def aclose(self) -> None: ...


class ProviderAdapter(Protocol):
    name: str

    async def chat(self, model: str, payload: dict[str, Any], timeout_s: float) -> ChatResult: ...
    async def open_stream(self, model: str, payload: dict[str, Any], timeout_s: float,
                          idle_timeout_s: float) -> StreamHandle: ...


def classify_status(status: int, provider: str, retry_after: Optional[str] = None) -> UpstreamError:
    msg = f"provider '{provider}' returned HTTP {status}"
    if status == 429:
        return UpstreamError("rate_limited", msg, status, retry_same=False, failover=True)
    if status >= 500:
        return UpstreamError("http", msg, status, retry_same=True, failover=True)
    # 4xx other than 429: our request or our config is wrong for this provider -> never retry the
    # same provider; let the chain try the next one (e.g. a misconfigured key/model on one provider).
    return UpstreamError("http", msg, status, retry_same=False, failover=True)


class _HttpxStream:
    def __init__(self, resp: httpx.Response, first: Optional[str], it: AsyncIterator[str]):
        self._resp, self.first, self._it = resp, first, it

    async def lines(self) -> AsyncIterator[str]:
        async for data in self._it:
            yield data

    async def aclose(self) -> None:
        await self._resp.aclose()


async def _sse_payloads(resp: httpx.Response) -> AsyncIterator[str]:
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].lstrip()


class OpenAICompatAdapter:
    def __init__(self, name: str, base_url: str, api_key: str, client: httpx.AsyncClient):
        self.name = name
        self._base = base_url.rstrip("/")
        self._key = api_key  # never logged, never returned to callers
        self._client = client

    def _req(self, model: str, payload: dict[str, Any], timeout: httpx.Timeout) -> httpx.Request:
        body = {**payload, "model": model}
        return self._client.build_request(
            "POST", f"{self._base}/chat/completions", json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {self._key}"})

    async def chat(self, model: str, payload: dict[str, Any], timeout_s: float) -> ChatResult:
        t = httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0))
        try:
            # wait_for = hard wall-clock cap even if a provider trickles bytes forever
            resp = await asyncio.wait_for(self._client.send(self._req(model, payload, t)), timeout_s + 0.25)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise UpstreamError("timeout", f"provider '{self.name}' timed out after {timeout_s:g}s",
                                retry_same=False, failover=True) from None
        except httpx.TransportError as e:
            raise UpstreamError("connect", f"provider '{self.name}' unreachable ({type(e).__name__})",
                                retry_same=True, failover=True) from None
        if resp.status_code != 200:
            raise classify_status(resp.status_code, self.name, resp.headers.get("retry-after"))
        try:
            body = resp.json()
            msg = body["choices"][0]["message"]
            usage = body["usage"]
            int(usage["prompt_tokens"]); int(usage["completion_tokens"])
            if not isinstance(msg.get("content"), str):
                raise ValueError("content is not a string")
        except (ValueError, KeyError, IndexError, TypeError):
            raise UpstreamError("malformed", f"provider '{self.name}' returned a malformed response",
                                retry_same=True, failover=True) from None
        return ChatResult(body)

    async def open_stream(self, model: str, payload: dict[str, Any], timeout_s: float,
                          idle_timeout_s: float) -> StreamHandle:
        # httpx `read` timeout applies between chunks once streaming; the first-byte bound is enforced below.
        t = httpx.Timeout(idle_timeout_s, connect=min(timeout_s, 2.0))
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        req = self._req(model, body, t)
        resp: Optional[httpx.Response] = None
        try:
            resp = await asyncio.wait_for(self._client.send(req, stream=True), timeout_s + 0.25)
            if resp.status_code != 200:
                await resp.aread()
                raise classify_status(resp.status_code, self.name)
            it = _sse_payloads(resp)
            first = await asyncio.wait_for(it.__anext__(), timeout_s)  # time-to-first-chunk bound
            return _HttpxStream(resp, first, it)
        except UpstreamError:
            if resp is not None:
                await resp.aclose()
            raise
        except StopAsyncIteration:
            if resp is not None:
                await resp.aclose()
            raise UpstreamError("malformed", f"provider '{self.name}' sent an empty stream",
                                retry_same=True, failover=True) from None
        except (asyncio.TimeoutError, httpx.TimeoutException):
            if resp is not None:
                await resp.aclose()
            raise UpstreamError("timeout", f"provider '{self.name}' timed out after {timeout_s:g}s",
                                retry_same=False, failover=True) from None
        except httpx.TransportError as e:
            if resp is not None:
                await resp.aclose()
            raise UpstreamError("connect", f"provider '{self.name}' unreachable ({type(e).__name__})",
                                retry_same=True, failover=True) from None


def parse_chunk(data: str) -> Optional[dict[str, Any]]:
    try:
        obj = json.loads(data)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None

"""Shared fixtures: a fake in-process provider (proves the adapter seam) and app builders."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
import pytest
import uvicorn

from prism.app import create_app
from prism.config import Settings, load_gateway_config
from prism.providers import ChatResult, UpstreamError

ROOT = Path(__file__).resolve().parents[1]
SEARCH, RESEARCH, FREE, BUDGET = (
    "prism-sk-search-1a2b3c", "prism-sk-research-4d5e6f", "prism-sk-free-7g8h9i", "prism-sk-budget-demo-0j1k2l")
ADMIN = {"x-admin-token": "prism-admin-demo"}


class FakeStream:
    def __init__(self, items: list, first: str, gate: Optional[threading.Event] = None, fail_after: Optional[int] = None):
        self.first, self._items, self._gate, self._fail_after = first, items, gate, fail_after

    async def lines(self) -> AsyncIterator[str]:
        for i, it in enumerate(self._items):
            if self._fail_after is not None and i >= self._fail_after:
                raise httpx.ReadError("connection reset mid-stream")
            if self._gate is not None and i == 0:
                while not self._gate.is_set():       # hold the rest of the stream until the test says go
                    await asyncio.sleep(0.01)
            yield it

    async def aclose(self) -> None:
        pass


class FakeProvider:
    """Behaves like a provider adapter; `mode` is flipped by tests to inject failures."""

    def __init__(self, name: str):
        self.name, self.mode, self.calls, self.delay = name, "ok", [], 0.0
        self.fail_first_n = 0
        self.gate: Optional[threading.Event] = None
        self.stream_fail_after: Optional[int] = None
        self.omit_usage = False

    def _reply(self, model: str, payload: dict) -> tuple[str, dict]:
        last = next((m["content"] for m in reversed(payload["messages"]) if m["role"] == "user"), "")
        text = f"[{model}] answer to: {last}"
        if "[refuse]" in str(last) and model.endswith("-small"):
            text = "I'm sorry, but I can't help with that."
        pt = sum(len(str(m.get("content", "")).split()) for m in payload["messages"])
        return text, {"prompt_tokens": pt, "completion_tokens": len(text.split()), "total_tokens": pt + len(text.split())}

    def _maybe_fail(self):
        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            raise UpstreamError("http", f"{self.name} flaked", 500, retry_same=True)
        if self.mode == "down":
            raise UpstreamError("http", f"{self.name} down", 503, retry_same=True)
        if self.mode == "timeout":
            raise UpstreamError("timeout", f"{self.name} timed out", retry_same=False)
        if self.mode == "ratelimited":
            raise UpstreamError("rate_limited", f"{self.name} 429", 429, retry_same=False)
        if self.mode == "malformed":
            raise UpstreamError("malformed", f"{self.name} malformed", retry_same=True)

    async def chat(self, model, payload, timeout_s) -> ChatResult:
        self.calls.append(model)
        if self.delay:
            await asyncio.sleep(self.delay)
        self._maybe_fail()
        text, usage = self._reply(model, payload)
        return ChatResult({"id": "c1", "object": "chat.completion", "created": 1, "model": model,
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                        "finish_reason": "stop"}], "usage": usage})

    async def open_stream(self, model, payload, timeout_s, idle_timeout_s):
        import json
        self.calls.append(model)
        self._maybe_fail()
        text, usage = self._reply(model, payload)
        words = text.split(" ")
        def chunk(delta, fin=None, u=None):
            d = {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            if u:
                d["usage"] = u
            return json.dumps(d)
        items = [chunk({"content": w + " "}) for w in words[1:]]
        items.append(chunk({}, "stop", None if self.omit_usage else usage))
        items.append("[DONE]")
        return FakeStream(items, chunk({"role": "assistant", "content": words[0] + " "}),
                          self.gate, self.stream_fail_after)


def make_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(data_dir=ROOT / "data", config_path=ROOT / "data" / "gateway_config.sample.json",
                    db_path=str(tmp_path / "t.sqlite3"), admin_token="prism-admin-demo", **kw)


def make_cfg(settings: Settings):
    cfg = load_gateway_config(settings.config_path, settings)
    cfg.retry.initial_backoff_ms = 1   # keep tests fast; backoff *shape* is unit-tested separately
    return cfg


@pytest.fixture
def providers():
    return {"alpha": FakeProvider("alpha"), "beta": FakeProvider("beta")}


@pytest.fixture
def client(tmp_path, providers):
    from starlette.testclient import TestClient
    s = make_settings(tmp_path)
    app = create_app(s, adapters=providers, cfg=make_cfg(s))
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


@pytest.fixture
def live_server(tmp_path, providers):
    """A real uvicorn server (so streaming can be observed on the wire, unbuffered)."""
    s = make_settings(tmp_path)
    app = create_app(s, adapters=providers, cfg=make_cfg(s))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started and server.servers:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    t.join(timeout=5)


def chat(c, key, content="What is a queue?", model="fast", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return c.post("/v1/chat/completions", json=body, headers={"authorization": f"Bearer {key}"})

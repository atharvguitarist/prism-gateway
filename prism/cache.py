"""Per-key semantic response cache.

Scoping (the multi-tenant safety rule)
--------------------------------------
Lookups only ever scan entries whose `virtual_key` equals the caller's key -- the
index is a dict keyed by `(virtual_key, scope)`, so another tenant's entries are not
merely filtered out, they are never visited. A cache hit therefore cannot leak a
different team's response. `scope` is the resolved alias/tier (a `fast` answer is never
served to a `smart` request), and `context_hash` pins everything *except* the final user
message (system prompt + earlier turns): the last user message is matched semantically,
the conversation context must match exactly. Single-turn prompts have an empty context.

Eviction: TTL (default 24h, lazily on lookup) + per-key LRU cap (default 500 entries).
Not cached: errors, refusals, non-`stop` finish reasons, tool/function calls, n>1, and
time-sensitive prompts ("current status", "today", "latest" ...).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .embedding import Embedder, LexicalConceptEmbedder, Vector, cosine
from .store import Store

_TIME_SENSITIVE = re.compile(
    r"\b(current(?:ly)?|right now|now|today|tonight|tomorrow|yesterday|latest|status|recent(?:ly)?|"
    r"breaking|live|up[- ]to[- ]date|this (?:morning|week|month|year)|weather|stock price|exchange rate|score)\b",
    re.I,
)
_REFUSAL = re.compile(
    r"^\s*(?:i['’]?m sorry|i am sorry|sorry|i apologi[sz]e)[^.\n]{0,60}(?:can['’]?t|cannot|unable|won['’]?t)\b", re.I)


def is_refusal(text: str) -> bool:
    return bool(_REFUSAL.search(text or ""))


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):  # OpenAI content parts
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c)
    return ""


def context_hash(messages: list[dict]) -> str:
    """Hash of everything before the final user message (system prompt + earlier turns)."""
    idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    ctx = messages[:idx] if idx >= 0 else messages
    if not ctx:
        return ""
    return hashlib.sha256(json.dumps(ctx, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def is_cacheable_request(body: dict, prompt: str) -> tuple[bool, str]:
    if any(body.get(k) for k in ("tools", "tool_choice", "functions", "function_call")):
        return False, "tool/function calling"
    if (body.get("n") or 1) != 1:
        return False, "n != 1"
    if _TIME_SENSITIVE.search(prompt):
        return False, "time-sensitive prompt"
    if not prompt.strip():
        return False, "empty prompt"
    return True, ""


@dataclass
class CacheEntry:
    id: int
    virtual_key: str
    scope: str
    context_hash: str
    vector: Vector
    prompt_text: str
    response: dict[str, Any]
    origin: str
    cost_nusd: int
    created_at: float
    last_hit_at: float
    hit_count: int = 0


@dataclass
class CacheHit:
    entry: CacheEntry
    similarity: float


class SemanticCache:
    def __init__(self, store: Store, embedder: Optional[Embedder] = None, ttl_seconds: int = 86400,
                 max_entries_per_key: int = 500, clock: Callable[[], float] = time.time):
        self.store = store
        self.embedder = embedder or LexicalConceptEmbedder()
        self.ttl = ttl_seconds
        self.max_entries = max_entries_per_key
        self._clock = clock
        self._index: dict[tuple[str, str], list[CacheEntry]] = {}
        self.reload()

    def reload(self) -> None:
        """(Re)build the in-memory index from the persistent store (startup, and after a key rotation)."""
        self._index = {}
        for r in self.store.load_cache_entries():
            e = CacheEntry(r["id"], r["virtual_key"], r["scope"], r["context_hash"], json.loads(r["vector"]),
                           r["prompt_text"], json.loads(r["response"]), r["origin"], r["cost_nusd"],
                           r["created_at"], r["last_hit_at"], r["hit_count"])
            self._index.setdefault((e.virtual_key, e.scope), []).append(e)

    def lookup(self, virtual_key: str, scope: str, messages: list[dict], threshold: float) -> Optional[CacheHit]:
        now = self._clock()
        bucket = self._index.get((virtual_key, scope))  # <-- tenant isolation: only this key's entries
        if not bucket:
            return None
        qvec = self.embedder.embed(last_user_text(messages))
        if not qvec:
            return None
        ctx = context_hash(messages)
        best: Optional[CacheHit] = None
        expired: list[CacheEntry] = []
        for e in bucket:
            if now - e.created_at > self.ttl:
                expired.append(e)
                continue
            if e.context_hash != ctx:
                continue
            sim = cosine(qvec, e.vector)
            if sim >= threshold and (best is None or sim > best.similarity):
                best = CacheHit(e, sim)
        if expired:
            self._drop(expired)
        if best:
            best.entry.hit_count += 1
            best.entry.last_hit_at = now
            self.store.touch_cache_entry(best.entry.id, now)
        return best

    def put(self, virtual_key: str, scope: str, messages: list[dict], response: dict[str, Any],
            origin: str, cost_nusd: int) -> Optional[CacheEntry]:
        text = last_user_text(messages)
        vec = self.embedder.embed(text)
        if not vec:
            return None
        ctx = context_hash(messages)
        bucket = self._index.setdefault((virtual_key, scope), [])
        for e in bucket:  # exact duplicate (same context + identical vector): refresh instead of growing
            if e.context_hash == ctx and cosine(vec, e.vector) > 0.9999:
                return e
        now = self._clock()
        eid = self.store.insert_cache_entry({
            "virtual_key": virtual_key, "scope": scope, "context_hash": ctx, "vector": json.dumps(vec),
            "prompt_text": text, "response": json.dumps(response), "origin": origin,
            "cost_nusd": cost_nusd, "created_at": now})
        entry = CacheEntry(eid, virtual_key, scope, ctx, vec, text, response, origin, cost_nusd, now, now)
        bucket.append(entry)
        self._enforce_cap(virtual_key)
        return entry

    def _enforce_cap(self, virtual_key: str) -> None:
        all_for_key = [e for (k, _), b in self._index.items() if k == virtual_key for e in b]
        overflow = len(all_for_key) - self.max_entries
        if overflow > 0:
            all_for_key.sort(key=lambda e: e.last_hit_at)  # least-recently-used first
            self._drop(all_for_key[:overflow])

    def _drop(self, entries: list[CacheEntry]) -> None:
        ids = {e.id for e in entries}
        for k, bucket in list(self._index.items()):
            self._index[k] = [e for e in bucket if e.id not in ids]
        self.store.delete_cache_entries(list(ids))

    def purge(self, virtual_key: Optional[str] = None) -> int:
        """Drop one key's entries (or all of them); returns how many were removed."""
        n = self.store.purge_cache_entries(virtual_key)
        self.reload()
        return n

    def size(self, virtual_key: Optional[str] = None) -> int:
        return sum(len(b) for (k, _), b in self._index.items() if virtual_key in (None, k))

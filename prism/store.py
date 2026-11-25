"""SQLite persistence: virtual keys, monthly usage, request log, cache entries.

Design notes
------------
* One connection in WAL mode. All access happens synchronously on the event-loop
  thread (statements are sub-millisecond, local file), so each `record_request()`
  call -- request-log insert + usage upsert -- is one atomic transaction and two
  concurrent requests can never lose an update.
* Usage is also kept in a dedicated `usage` table keyed by (key, month) so budget
  enforcement is a single primary-key read, never a scan of the log.
* All money is integer nano-USD (see pricing.py).
* Month window = UTC calendar month; a new month simply has no row yet, which
  *is* the "reset-on-read" budget reset.
* No prompt/response bodies are written to the request log (privacy by default).
  Bodies exist only in `cache_entries`, scoped per key, with a TTL.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .pricing import nusd_to_usd, usd_to_nusd

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    virtual_key        TEXT PRIMARY KEY,
    team               TEXT NOT NULL,
    monthly_budget_nusd INTEGER NOT NULL,
    rpm                INTEGER NOT NULL,
    tpm                INTEGER,
    allowlist          TEXT NOT NULL,
    cache_enabled      INTEGER NOT NULL DEFAULT 0,
    cache_threshold    REAL,
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    virtual_key        TEXT NOT NULL,
    month              TEXT NOT NULL,
    requests           INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_nusd          INTEGER NOT NULL DEFAULT 0,
    cache_hits         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (virtual_key, month)
);
CREATE TABLE IF NOT EXISTS request_logs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         TEXT NOT NULL,
    virtual_key        TEXT,
    requested_model    TEXT,
    resolved_provider  TEXT,
    resolved_model     TEXT,
    status             TEXT NOT NULL,
    http_status        INTEGER,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_nusd          INTEGER NOT NULL DEFAULT 0,
    cache              TEXT NOT NULL DEFAULT 'miss',
    cache_lookup       INTEGER NOT NULL DEFAULT 0,
    saved_nusd         INTEGER NOT NULL DEFAULT 0,
    fallback           INTEGER NOT NULL DEFAULT 0,
    escalated          INTEGER NOT NULL DEFAULT 0,
    route_reason       TEXT,
    retries            INTEGER NOT NULL DEFAULT 0,
    stream             INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    upstream_ms        INTEGER NOT NULL DEFAULT 0,
    overhead_ms        INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    flags              TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_key_time ON request_logs (virtual_key, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_time ON request_logs (created_at);
CREATE TABLE IF NOT EXISTS cache_entries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_key        TEXT NOT NULL,
    scope              TEXT NOT NULL,
    context_hash       TEXT NOT NULL,
    vector             TEXT NOT NULL,
    prompt_text        TEXT NOT NULL,
    response           TEXT NOT NULL,
    origin             TEXT NOT NULL,
    cost_nusd          INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    last_hit_at        REAL NOT NULL,
    hit_count          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_key ON cache_entries (virtual_key);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def month_of(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m")


@dataclass
class Tenant:
    virtual_key: str
    team: str
    monthly_budget_nusd: int
    rpm: int
    tpm: Optional[int]
    allowlist: list[str]
    cache_enabled: bool
    cache_threshold: Optional[float]
    status: str
    created_at: str

    @property
    def active(self) -> bool:
        return self.status == "active"

    def public(self) -> dict[str, Any]:
        return {
            "virtual_key": self.virtual_key,
            "team": self.team,
            "monthly_budget_usd": nusd_to_usd(self.monthly_budget_nusd),
            "rate_limit": {"requests_per_minute": self.rpm, "tokens_per_minute": self.tpm},
            "model_allowlist": self.allowlist,
            "semantic_cache": {"enabled": self.cache_enabled, "similarity_threshold": self.cache_threshold},
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class LogEntry:
    request_id: str
    virtual_key: Optional[str]
    requested_model: Optional[str]
    status: str
    http_status: int
    resolved_provider: Optional[str] = None
    resolved_model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_nusd: int = 0
    cache: str = "miss"
    cache_lookup: bool = False
    saved_nusd: int = 0
    fallback: bool = False
    escalated: bool = False
    route_reason: Optional[str] = None
    retries: int = 0
    stream: bool = False
    latency_ms: int = 0
    upstream_ms: int = 0
    overhead_ms: int = 0
    error: Optional[str] = None
    flags: Optional[str] = None      # comma-separated guardrail / circuit-breaker events, e.g. 'pii_redacted:email'

    @property
    def served(self) -> bool:
        return self.status in ("ok", "cache_hit")


def _tenant_from_row(r: sqlite3.Row) -> Tenant:
    return Tenant(
        virtual_key=r["virtual_key"], team=r["team"], monthly_budget_nusd=r["monthly_budget_nusd"],
        rpm=r["rpm"], tpm=r["tpm"], allowlist=json.loads(r["allowlist"]),
        cache_enabled=bool(r["cache_enabled"]), cache_threshold=r["cache_threshold"],
        status=r["status"], created_at=r["created_at"],
    )


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()  # belt and braces: also safe if used from threads
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(request_logs)")}
            if "flags" not in cols:   # databases created before guardrails / circuit breaker existed
                self._conn.execute("ALTER TABLE request_logs ADD COLUMN flags TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- keys
    def seed_tenants(self, tenants: Iterable[dict], default_threshold: float) -> int:
        """Insert seed tenants that do not exist yet. Existing rows are left untouched so
        that state changed via the admin API (disabled / rotated keys) survives restarts."""
        n = 0
        with self._lock:
            for t in tenants:
                sc = t.get("semantic_cache", {}) or {}
                enabled = bool(sc.get("enabled", False))
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO keys (virtual_key, team, monthly_budget_nusd, rpm, tpm, allowlist,"
                    " cache_enabled, cache_threshold, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        t["virtual_key"], t["team"], usd_to_nusd(t["monthly_budget_usd"]),
                        t["rate_limit"]["requests_per_minute"], t["rate_limit"].get("tokens_per_minute"),
                        json.dumps(t["model_allowlist"]), int(enabled),
                        sc.get("similarity_threshold", default_threshold) if enabled else None,
                        t.get("status", "active"), iso(utcnow()),
                    ),
                )
                n += cur.rowcount
        return n

    def get_tenant(self, virtual_key: str) -> Optional[Tenant]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM keys WHERE virtual_key=?", (virtual_key,)).fetchone()
        return _tenant_from_row(r) if r else None

    def list_tenants(self) -> list[Tenant]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM keys ORDER BY created_at, virtual_key").fetchall()
        return [_tenant_from_row(r) for r in rows]

    def create_tenant(self, spec: dict, default_threshold: float) -> Tenant:
        slug = "".join(c for c in spec["team"].lower() if c.isalnum() or c == "-")[:20] or "team"
        vk = f"prism-sk-{slug}-{secrets.token_hex(6)}"
        self.seed_tenants([{**spec, "virtual_key": vk}], default_threshold)
        return self.get_tenant(vk)  # type: ignore[return-value]

    def update_tenant(self, virtual_key: str, changes: dict) -> Optional[Tenant]:
        """Apply a validated partial update (see app.parse_key_patch). Takes effect on the next request."""
        sets: list[str] = []
        args: list[Any] = []
        if "team" in changes:
            sets.append("team=?"); args.append(changes["team"])
        if "monthly_budget_usd" in changes:
            sets.append("monthly_budget_nusd=?"); args.append(usd_to_nusd(changes["monthly_budget_usd"]))
        rl = changes.get("rate_limit") or {}
        if "requests_per_minute" in rl:
            sets.append("rpm=?"); args.append(rl["requests_per_minute"])
        if "tokens_per_minute" in rl:
            sets.append("tpm=?"); args.append(rl["tokens_per_minute"])
        if "model_allowlist" in changes:
            sets.append("allowlist=?"); args.append(json.dumps(changes["model_allowlist"]))
        sc = changes.get("semantic_cache")
        if sc is not None:
            if "enabled" in sc:
                sets.append("cache_enabled=?"); args.append(int(bool(sc["enabled"])))
            if "similarity_threshold" in sc:
                sets.append("cache_threshold=?"); args.append(sc["similarity_threshold"])
        with self._lock:
            if self._conn.execute("SELECT 1 FROM keys WHERE virtual_key=?", (virtual_key,)).fetchone() is None:
                return None
            if sets:
                self._conn.execute(f"UPDATE keys SET {', '.join(sets)} WHERE virtual_key=?", (*args, virtual_key))
        return self.get_tenant(virtual_key)

    def set_status(self, virtual_key: str, status: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "UPDATE keys SET status=? WHERE virtual_key=?", (status, virtual_key)).rowcount > 0

    def rotate_key(self, old: str) -> Optional[str]:
        """Issue a new key value for the same tenant; history follows the tenant, old value dies instantly."""
        new = f"{old.rsplit('-', 1)[0]}-{secrets.token_hex(6)}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("UPDATE keys SET virtual_key=? WHERE virtual_key=?", (new, old)).rowcount == 0:
                    self._conn.execute("ROLLBACK")
                    return None
                for table in ("usage", "request_logs", "cache_entries"):
                    self._conn.execute(f"UPDATE {table} SET virtual_key=? WHERE virtual_key=?", (new, old))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return new

    # --------------------------------------------------------------- usage
    def month_spend_nusd(self, virtual_key: str, month: Optional[str] = None) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT cost_nusd FROM usage WHERE virtual_key=? AND month=?",
                (virtual_key, month or month_of())).fetchone()
        return r["cost_nusd"] if r else 0

    def record_request(self, e: LogEntry, when: Optional[datetime] = None) -> None:
        """Atomically append the request-log row and bump the monthly usage counters."""
        when = when or utcnow()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO request_logs (request_id, virtual_key, requested_model, resolved_provider,"
                    " resolved_model, status, http_status, prompt_tokens, completion_tokens, cost_nusd, cache,"
                    " cache_lookup, saved_nusd, fallback, escalated, route_reason, retries, stream, latency_ms,"
                    " upstream_ms, overhead_ms, error, flags, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e.request_id, e.virtual_key, e.requested_model, e.resolved_provider, e.resolved_model,
                     e.status, e.http_status, e.prompt_tokens, e.completion_tokens, e.cost_nusd, e.cache,
                     int(e.cache_lookup), e.saved_nusd, int(e.fallback), int(e.escalated), e.route_reason,
                     e.retries, int(e.stream), e.latency_ms, e.upstream_ms, e.overhead_ms, e.error, e.flags, iso(when)),
                )
                if e.virtual_key and (e.served or e.cost_nusd or e.prompt_tokens or e.completion_tokens):
                    self._conn.execute(
                        "INSERT INTO usage (virtual_key, month, requests, prompt_tokens, completion_tokens,"
                        " cost_nusd, cache_hits) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(virtual_key, month) DO UPDATE SET "
                        " requests = requests + excluded.requests,"
                        " prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                        " completion_tokens = completion_tokens + excluded.completion_tokens,"
                        " cost_nusd = cost_nusd + excluded.cost_nusd,"
                        " cache_hits = cache_hits + excluded.cache_hits",
                        (e.virtual_key, month_of(when), 1 if e.served else 0, e.prompt_tokens,
                         e.completion_tokens, e.cost_nusd, 1 if e.status == "cache_hit" else 0),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------- reports
    @staticmethod
    def _window(frm: Optional[str], to: Optional[str]) -> tuple[str, str, str, str]:
        today = utcnow().date()
        f = datetime.strptime(frm, "%Y-%m-%d").date() if frm else today.replace(day=1)
        t = datetime.strptime(to, "%Y-%m-%d").date() if to else today
        return f.isoformat(), t.isoformat(), f"{f.isoformat()}T00:00:00.000Z", \
            f"{(t + timedelta(days=1)).isoformat()}T00:00:00.000Z"

    def usage_summary(self, virtual_key: Optional[str], frm: Optional[str], to: Optional[str]) -> dict[str, Any]:
        f, t, lo, hi = self._window(frm, to)
        where, args = "created_at >= ? AND created_at < ?", [lo, hi]
        if virtual_key:
            where += " AND virtual_key = ?"
            args.append(virtual_key)
        with self._lock:
            tot = self._conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status IN ('ok','cache_hit') THEN 1 ELSE 0 END),0) AS requests,"
                " COALESCE(SUM(CASE WHEN status LIKE 'rejected%' THEN 1 ELSE 0 END),0) AS rejected,"
                " COALESCE(SUM(CASE WHEN status IN ('upstream_error','stream_error','stream_aborted') THEN 1 ELSE 0 END),0) AS errors,"
                " COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct,"
                " COALESCE(SUM(cost_nusd),0) AS cost, COALESCE(SUM(CASE WHEN status='cache_hit' THEN 1 ELSE 0 END),0) AS hits,"
                " COALESCE(SUM(saved_nusd),0) AS saved "
                f"FROM request_logs WHERE {where}", args).fetchone()
            by_model = self._conn.execute(
                "SELECT resolved_model AS model, COUNT(*) AS requests, SUM(prompt_tokens) AS pt,"
                " SUM(completion_tokens) AS ct, SUM(cost_nusd) AS cost FROM request_logs "
                f"WHERE {where} AND resolved_model IS NOT NULL AND status IN ('ok','stream_error','stream_aborted') "
                "GROUP BY resolved_model ORDER BY cost DESC", args).fetchall()
            by_day = self._conn.execute(
                "SELECT substr(created_at,1,10) AS day, SUM(CASE WHEN status IN ('ok','cache_hit') THEN 1 ELSE 0 END) AS requests,"
                " SUM(cost_nusd) AS cost FROM request_logs "
                f"WHERE {where} GROUP BY day ORDER BY day", args).fetchall()
        out: dict[str, Any] = {
            "from": f, "to": t,
            "requests": tot["requests"], "rejected": tot["rejected"], "errors": tot["errors"],
            "prompt_tokens": tot["pt"], "completion_tokens": tot["ct"],
            "cost_usd": nusd_to_usd(tot["cost"]), "cache_hits": tot["hits"],
            "cache_saved_usd": nusd_to_usd(tot["saved"]),
            "by_model": [{"model": r["model"], "requests": r["requests"], "prompt_tokens": r["pt"],
                          "completion_tokens": r["ct"], "cost_usd": nusd_to_usd(r["cost"])} for r in by_model],
            "by_day": [{"day": r["day"], "requests": r["requests"], "cost_usd": nusd_to_usd(r["cost"] or 0)}
                       for r in by_day],
        }
        if virtual_key:
            out = {"key": virtual_key, **out}
        return out

    def usage_by_key(self) -> list[dict[str, Any]]:
        """Current-month rollup per tenant, straight from the `usage` table (what budgets are enforced on)."""
        month = month_of()
        with self._lock:
            rows = self._conn.execute(
                "SELECT k.virtual_key, k.team, k.status, k.monthly_budget_nusd, k.rpm,"
                " COALESCE(u.requests,0) AS requests, COALESCE(u.prompt_tokens,0) AS pt,"
                " COALESCE(u.completion_tokens,0) AS ct, COALESCE(u.cost_nusd,0) AS cost,"
                " COALESCE(u.cache_hits,0) AS hits "
                "FROM keys k LEFT JOIN usage u ON u.virtual_key=k.virtual_key AND u.month=? "
                "ORDER BY k.created_at, k.virtual_key", (month,)).fetchall()
        return [{
            "key": r["virtual_key"], "team": r["team"], "status": r["status"], "month": month,
            "requests": r["requests"], "prompt_tokens": r["pt"], "completion_tokens": r["ct"],
            "cost_usd": nusd_to_usd(r["cost"]), "cache_hits": r["hits"],
            "budget_usd": nusd_to_usd(r["monthly_budget_nusd"]),
            "budget_used_pct": round(100 * r["cost"] / r["monthly_budget_nusd"], 2) if r["monthly_budget_nusd"] else None,
            "rpm_limit": r["rpm"],
        } for r in rows]

    def recent_logs(self, virtual_key: Optional[str] = None, limit: int = 50,
                    status: Optional[str] = None, routed_only: bool = False) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if virtual_key:
            where.append("virtual_key = ?"); args.append(virtual_key)
        if status:
            where.append("status = ?"); args.append(status)
        if routed_only:
            where.append("route_reason IS NOT NULL")
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM request_logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["cost_usd"] = nusd_to_usd(d.pop("cost_nusd"))
            d["saved_usd"] = nusd_to_usd(d.pop("saved_nusd"))
            for b in ("fallback", "escalated", "stream"):
                d[b] = bool(d[b])
            d.pop("cache_lookup", None)
            out.append(d)
        return out

    def cache_stats(self, virtual_key: Optional[str] = None) -> dict[str, Any]:
        where, args = "created_at >= ? AND cache_lookup = 1", [self._window(None, None)[2]]
        if virtual_key:
            where += " AND virtual_key = ?"; args.append(virtual_key)
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS lookups, COALESCE(SUM(CASE WHEN cache='hit' THEN 1 ELSE 0 END),0) AS hits,"
                " COALESCE(SUM(saved_nusd),0) AS saved FROM request_logs WHERE " + where, args).fetchone()
            ent_args = [virtual_key] if virtual_key else []
            ent = self._conn.execute(
                "SELECT COUNT(*) AS n FROM cache_entries" + (" WHERE virtual_key=?" if virtual_key else ""),
                ent_args).fetchone()
            per_key = self._conn.execute(
                "SELECT virtual_key, COUNT(*) AS lookups, SUM(CASE WHEN cache='hit' THEN 1 ELSE 0 END) AS hits "
                "FROM request_logs WHERE created_at >= ? AND cache_lookup = 1 GROUP BY virtual_key",
                [self._window(None, None)[2]]).fetchall()
        lookups, hits = r["lookups"], r["hits"]
        return {
            "scope": virtual_key or "all", "window": "current month (UTC)",
            "lookups": lookups, "hits": hits, "misses": lookups - hits,
            "hit_rate": round(hits / lookups, 4) if lookups else 0.0,
            "cost_saved_usd": nusd_to_usd(r["saved"]), "entries": ent["n"],
            "by_key": [{"key": p["virtual_key"], "lookups": p["lookups"], "hits": p["hits"],
                        "hit_rate": round(p["hits"] / p["lookups"], 4) if p["lookups"] else 0.0}
                       for p in per_key],
        }

    def status_counts(self) -> dict[str, int]:
        """Requests by outcome for the current UTC month (feeds /metrics)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM request_logs WHERE created_at >= ? GROUP BY status",
                (self._window(None, None)[2],)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def guardrail_counts(self) -> dict[str, int]:
        """Guardrail / circuit-breaker events (the `flags` column) for the current UTC month."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT flags, COUNT(*) AS n FROM request_logs WHERE created_at >= ? AND flags IS NOT NULL "
                "GROUP BY flags", (self._window(None, None)[2],)).fetchall()
        out: dict[str, int] = {}
        for r in rows:
            for ev in r["flags"].split(","):
                if ev:
                    out[ev] = out.get(ev, 0) + r["n"]
        return out

    def provider_stats(self, window_s: int = 300) -> list[dict[str, Any]]:
        since = iso(utcnow() - timedelta(seconds=window_s))
        with self._lock:
            rows = self._conn.execute(
                "SELECT resolved_provider AS p, COUNT(*) AS n, AVG(upstream_ms) AS avg_ms, MAX(upstream_ms) AS max_ms "
                "FROM request_logs WHERE created_at >= ? AND resolved_provider IS NOT NULL AND status='ok' "
                "GROUP BY resolved_provider", (since,)).fetchall()
        return [dict(r) for r in rows]

    def overhead_percentiles(self, since: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT overhead_ms FROM request_logs WHERE status='ok' AND created_at >= ? ORDER BY overhead_ms",
                (since or "1970-01-01T00:00:00.000Z",)).fetchall()
        v = [r["overhead_ms"] for r in rows]
        if not v:
            return {"n": 0}
        pick = lambda q: v[min(len(v) - 1, int(q * len(v)))]
        return {"n": len(v), "p50_ms": pick(0.50), "p95_ms": pick(0.95), "max_ms": v[-1]}

    # --------------------------------------------------------------- cache
    def load_cache_entries(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM cache_entries").fetchall()

    def insert_cache_entry(self, row: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cache_entries (virtual_key, scope, context_hash, vector, prompt_text, response,"
                " origin, cost_nusd, created_at, last_hit_at, hit_count) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (row["virtual_key"], row["scope"], row["context_hash"], row["vector"], row["prompt_text"],
                 row["response"], row["origin"], row["cost_nusd"], row["created_at"], row["created_at"]))
            return cur.lastrowid  # type: ignore[return-value]

    def touch_cache_entry(self, entry_id: int, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cache_entries SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?", (now, entry_id))

    def purge_cache_entries(self, virtual_key: Optional[str] = None) -> int:
        with self._lock:
            if virtual_key:
                return self._conn.execute("DELETE FROM cache_entries WHERE virtual_key=?", (virtual_key,)).rowcount
            return self._conn.execute("DELETE FROM cache_entries").rowcount

    def delete_cache_entries(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM cache_entries WHERE id = ?", [(i,) for i in ids])

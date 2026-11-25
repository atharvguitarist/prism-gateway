"""Settings and gateway configuration loading.

Environment variables (all optional):

    PRISM_DATA_DIR              directory holding model_pricing.json / seed_keys.json (default: ./data)
    PRISM_CONFIG                gateway config JSON (default: <data dir>/gateway_config.sample.json)
    PRISM_DB                    SQLite file (default: ./prism.sqlite3)
    PRISM_ADMIN_TOKEN           admin-plane token (default: prism-admin-demo  -- change it!)
    PRISM_UPSTREAM_TIMEOUT_MS   per-attempt upstream timeout / time-to-first-byte (default: 2000)
    PRISM_STREAM_IDLE_TIMEOUT_MS  max silence between stream chunks (default: 5000)
    PRISM_PROVIDER_<NAME>_API_KEY / PRISM_PROVIDER_<NAME>_BASE_URL  override a provider's secret / URL
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = field(repr=False)  # secret: repr=False keeps it out of logs/tracebacks
    models: Optional[list[str]] = None  # optional explicit model list; else "<name>-*" prefix convention


@dataclass
class AliasConfig:
    alias: str
    primary: Optional[str] = None
    fallbacks: list[str] = field(default_factory=list)
    route_by_difficulty: Optional[dict[str, str]] = None  # only for the `auto` alias

    @property
    def chain(self) -> list[str]:
        return [self.primary, *self.fallbacks] if self.primary else []


@dataclass
class RetryConfig:
    max_attempts: int = 3          # attempts per provider (for retryable errors)
    initial_backoff_ms: int = 200
    backoff_multiplier: float = 2.0
    max_total_ms: int = 15000      # hard cap on retries+failover for one request


@dataclass
class CacheConfig:
    ttl_seconds: int = 24 * 3600
    max_entries_per_key: int = 500
    default_threshold: float = 0.90


@dataclass
class BreakerConfig:
    enabled: bool = True
    failure_threshold: int = 3     # consecutive provider failures that open the circuit
    cooldown_s: float = 5.0        # time out of rotation before one probe request is let through


@dataclass
class GuardrailConfig:
    pii: str = "redact"            # off | redact | block
    injection: str = "flag"        # off | flag | block


@dataclass
class GatewayConfig:
    providers: dict[str, ProviderConfig]
    aliases: dict[str, AliasConfig]
    retry: RetryConfig
    cache: CacheConfig
    escalation_enabled: bool = True
    upstream_timeout_s: float = 2.0
    stream_idle_timeout_s: float = 5.0
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)

    def provider_for_model(self, model: str) -> Optional[ProviderConfig]:
        for p in self.providers.values():
            if p.models is not None and model in p.models:
                return p
        for p in self.providers.values():
            if p.models is None and model.startswith(p.name + "-"):
                return p
        return None


@dataclass
class Settings:
    data_dir: Path
    config_path: Path
    db_path: str
    admin_token: str
    upstream_timeout_ms: int = 2000
    stream_idle_timeout_ms: int = 5000

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("PRISM_DATA_DIR", "data"))
        return cls(
            data_dir=data_dir,
            config_path=Path(os.environ.get("PRISM_CONFIG", data_dir / "gateway_config.sample.json")),
            db_path=os.environ.get("PRISM_DB", "prism.sqlite3"),
            admin_token=os.environ.get("PRISM_ADMIN_TOKEN", "prism-admin-demo"),
            upstream_timeout_ms=int(os.environ.get("PRISM_UPSTREAM_TIMEOUT_MS", "2000")),
            stream_idle_timeout_ms=int(os.environ.get("PRISM_STREAM_IDLE_TIMEOUT_MS", "5000")),
        )


def load_pricing(path: Path) -> dict[str, dict[str, float]]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {m: p for m, p in raw.items() if not m.startswith("_")}


def load_gateway_config(path: Path, settings: Optional[Settings] = None) -> GatewayConfig:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    providers: dict[str, ProviderConfig] = {}
    for p in raw["providers"]:
        env = "PRISM_PROVIDER_" + p["name"].upper().replace("-", "_")
        providers[p["name"]] = ProviderConfig(
            name=p["name"],
            base_url=os.environ.get(env + "_BASE_URL", p["base_url"]).rstrip("/"),
            api_key=os.environ.get(env + "_API_KEY", p["api_key"]),
            models=p.get("models"),
        )

    aliases: dict[str, AliasConfig] = {}
    for name, a in raw["model_aliases"].items():
        aliases[name] = AliasConfig(
            alias=name,
            primary=a.get("primary"),
            fallbacks=list(a.get("fallbacks", [])),
            route_by_difficulty=a.get("route_by_difficulty"),
        )

    r = raw.get("retry", {})
    retry = RetryConfig(
        max_attempts=r.get("max_attempts", 3),
        initial_backoff_ms=r.get("initial_backoff_ms", 200),
        backoff_multiplier=r.get("backoff_multiplier", 2),
        max_total_ms=r.get("max_total_ms", 15000),
    )
    c = raw.get("cache", {})
    cache = CacheConfig(
        ttl_seconds=c.get("ttl_seconds", 24 * 3600),
        max_entries_per_key=c.get("max_entries_per_key", 500),
        default_threshold=c.get("default_threshold", 0.90),
    )
    t = raw.get("timeouts", {})
    up_ms = t.get("upstream_ms", settings.upstream_timeout_ms if settings else 2000)
    idle_ms = t.get("stream_idle_ms", settings.stream_idle_timeout_ms if settings else 5000)
    if settings:  # explicit env var wins over file
        if "PRISM_UPSTREAM_TIMEOUT_MS" in os.environ:
            up_ms = settings.upstream_timeout_ms
        if "PRISM_STREAM_IDLE_TIMEOUT_MS" in os.environ:
            idle_ms = settings.stream_idle_timeout_ms

    b = raw.get("circuit_breaker", {})
    breaker = BreakerConfig(
        enabled=bool(b.get("enabled", True)),
        failure_threshold=int(b.get("failure_threshold", 3)),
        cooldown_s=float(b.get("cooldown_s", 5.0)),
    )
    g = raw.get("guardrails", {})
    guardrails = GuardrailConfig(pii=g.get("pii", "redact"), injection=g.get("injection", "flag"))
    if guardrails.pii not in ("off", "redact", "block"):
        raise ValueError(f"guardrails.pii must be off|redact|block, got '{guardrails.pii}'")
    if guardrails.injection not in ("off", "flag", "block"):
        raise ValueError(f"guardrails.injection must be off|flag|block, got '{guardrails.injection}'")

    return GatewayConfig(
        providers=providers,
        aliases=aliases,
        retry=retry,
        cache=cache,
        escalation_enabled=raw.get("escalation", {}).get("enabled", True),
        upstream_timeout_s=up_ms / 1000,
        stream_idle_timeout_s=idle_ms / 1000,
        breaker=breaker,
        guardrails=guardrails,
    )

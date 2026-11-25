"""Difficulty classifier behind the `auto` alias.

Method: an offline, engineered-signal scorer (no model call, ~0.1 ms, deterministic).

Key idea -- **classify the task, not the prompt**. Length is a bad proxy for difficulty:
"Prove that sqrt(2) is irrational." is short and hard, while a 100-word roster followed by
"Who is on call Thursday?" is long and trivial. So for long prompts we score only the
*instruction-bearing* sentences (first sentence + last two), and ignore pasted context/data.

Score = sum of weighted regex signals.
  hard signals (+): proofs/derivations, estimation, design/architecture, planning/migration,
                    comparison + recommendation, diagnosis/"why might", distributed-systems
                    and concurrency vocabulary, algorithmic complexity, code-writing,
                    analysis, multi-part requests ("... and explain", "give three", "in order of").
  easy signals (-): lookups ("what is the capital..."), convert/translate/extract/rewrite,
                    one-line/yes-no outputs, simple arithmetic, definitions.
  score >= HARD_THRESHOLD -> `smart`, else `fast`.

The labels in data/routing_eval*.jsonl are never read by this module. Every decision
returns the matched signals so the request log can say *why* (route_reason).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HARD_THRESHOLD = 2.0
LONG_PROMPT_WORDS = 35

# (name, regex, weight, cap_group)  -- signals sharing a cap_group are capped together
_HARD: list[tuple[str, str, float, str | None]] = [
    ("proof/derivation", r"\b(prove|disprove|proof|show that|demonstrate that|derive|derivation)\b", 3.0, None),
    ("show-reasoning", r"\b(show your (reasoning|work)|step[- ]by[- ]step reasoning|justify|justification|walk through|reason about)\b", 2.0, None),
    ("estimation", r"\bestimate\b|\bfermi\b|\bapproximately how many\b", 2.0, None),
    ("design/architecture", r"\b(design|architect|architecture|schema|data model|system design)\b", 2.0, None),
    ("planning/migration", r"\b(migrat\w+|rollback|roll back|zero[- ]downtime|phased|roadmap|rollout)\b", 1.5, None),
    ("plan/strategy", r"\b(outline|draft|propose|devise|develop|create)\b.{0,30}\b(plan|strategy)\b", 1.5, None),
    ("comparison", r"\b(compare|contrast|versus|trade-?offs?|pros and cons|differences? between|which is better)\b", 1.5, None),
    ("recommendation", r"\b(recommend|should (we|i) (choose|use|pick|adopt)|would you choose)\b", 1.0, None),
    ("diagnosis", r"\b(root cause|likely causes?|diagnos\w+|debug\w*|troubleshoot\w*|hypothes[ei]s|systematically find)\b", 2.0, None),
    ("why-question", r"\bwhy (might|can|could|would|does|do|did|is|are|should)\b", 1.5, None),
    ("mechanisms", r"\b(mechanisms?|failure (modes?|dynamics)|anomal(y|ies)|amplif\w+|edge cases?)\b", 1.5, None),
    ("systems-vocab", r"\b(race conditions?|deadlocks?|lost updates?|snapshot isolation|isolation level|consistency|consensus|quorum|"
                      r"partitions?|idempoten\w+|backpressure|p99|p95|tail latency|throughput|scalab\w+|concurren\w+|"
                      r"distributed|replication|sharding|leaks?|retry|retries|backoff|latency|event[- ]driven|durability)\b", 1.5, "sys"),
    ("algorithmic", r"\b(algorithms?|time complexity|complexity|big[- ]o|o\(\w|better than o|data structures?|dynamic programming|recursion)\b", 2.0, None),
    ("write-code", r"\b(write|implement|code|refactor|optimi[sz]e)\b.{0,40}\b(function|class|algorithm|query|program|script|service|module|cache|parser)\b", 1.5, None),
    ("analysis", r"\b(analy[sz]e|analysis|evaluate|assess|critique|implications?|security implications?)\b", 1.5, None),
    ("explain-why/how", r"\bexplain (why|how|what|the|your)\b|\bexplain\b", 1.0, None),
    ("multi-part", r"\b(and (also )?(explain|recommend|justify|discuss|describe)|including how|with justification|"
                   r"give (me )?(three|3|four|several|a few|\d+)|list the most|in order of|ranked list|at each (stage|step)|"
                   r"for each|concrete example|what you give up|and how (do|would|should|can) you)\b", 1.0, "multi"),
    ("multi-tenant/saas", r"\b(multi-tenant|saas|microservices?|monolith(ic)?|distributed system)\b", 1.5, None),
]

_EASY: list[tuple[str, str, float]] = [
    ("convert/translate/extract/rewrite", r"\b(convert|translate|extract|rewrite|rephrase|paraphrase|reformat|format (this|the)|proofread|"
                                          r"spell|fix the typo|correct the grammar|shorten|make (this|it|the sentence) (shorter|longer|polite|formal))\b", 2.5),
    ("one-liner output", r"\b(one[- ]line|one[- ]sentence|one[- ]word|single sentence|in a sentence|yes or no|commit message|tagline|greeting|haiku)\b", 2.0),
    ("simple arithmetic", r"\bis \d+ (divisible|prime|even|odd)\b|\b\d+\s*[\+\-\*/×x]\s*\d+\b|\bhow many \w+ (are )?in \d", 2.0),
    ("definition/acronym", r"\bdefine\b|\bwhat does [\w'\-\. ]{1,30}(mean|stand for)\b|\bacronym\b|\bnoun or\b|\bwho wrote\b", 2.0),
    ("simple lookup", r"^\s*(what|who|which|when|where)\s+(is|are|was|were|does|did)\b", 1.5),
    ("how-many/which/time lookup", r"\b(how many|how much|which (region|one|day|planet|country)|what time|what day|who is on)\b|\bwhat is the (first|last|name|port|date|time|timestamp|capital)\b", 1.5),
    ("kilometers/units", r"\b(kilometers?|miles?|celsius|fahrenheit|kilograms?|pounds?|liters?|gallons?)\b", 1.0),
]

_HARD_RX = [(n, re.compile(p, re.I), w, g) for n, p, w, g in _HARD]
_EASY_RX = [(n, re.compile(p, re.I), w) for n, p, w in _EASY]
_CAPS = {"sys": 3.0, "multi": 2.0}
_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class RouteDecision:
    tier: str                    # "fast" | "smart"
    score: float
    reason: str
    signals: list[str] = field(default_factory=list)
    scored_text: str = ""

    def short_reason(self) -> str:
        return f"auto->{self.tier} (score {self.score:+.1f}): {self.reason}"


def task_segment(prompt: str) -> tuple[str, bool]:
    """Return (text to score, truncated?). Long prompts: first sentence + last two sentences."""
    words = prompt.split()
    if len(words) <= LONG_PROMPT_WORDS:
        return prompt, False
    sents = [s for s in _SENT.split(prompt.strip()) if s]
    if len(sents) <= 3:
        return prompt, False
    return " ".join([sents[0], *sents[-2:]]), True


def classify(prompt: str) -> RouteDecision:
    text, truncated = task_segment(prompt)
    score = 0.0
    hits: list[str] = []
    grouped: dict[str, float] = {}
    for name, rx, weight, group in _HARD_RX:
        n = len(set(m.group(0).lower() for m in rx.finditer(text)))
        if not n:
            continue
        if group:
            add = min(weight * n, _CAPS[group] - grouped.get(group, 0.0))
            if add <= 0:
                continue
            grouped[group] = grouped.get(group, 0.0) + add
        else:
            add = weight
        score += add
        hits.append(f"{name} +{add:g}")
    for name, rx, weight in _EASY_RX:
        if rx.search(text):
            score -= weight
            hits.append(f"{name} -{weight:g}")
    tier = "smart" if score >= HARD_THRESHOLD else "fast"
    why = ", ".join(hits) if hits else "no difficulty signals"
    if truncated:
        why += " [scored instruction sentences only; long context ignored]"
    return RouteDecision(tier=tier, score=round(score, 2), reason=why, signals=hits, scored_text=text)

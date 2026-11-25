"""Request guardrails: PII / secret detection and prompt-injection heuristics.

Runs on the request *before* the cache and before any provider sees it, so redacted text is also what
gets cached and what gets sent upstream. Everything here is deterministic regex + checksums:
no model call, ~tens of microseconds, and every detection is explainable.

Modes (gateway config `guardrails`):
    pii:        off | redact | block      (default: redact)
    injection:  off | flag | block        (default: flag)

Detected PII / secrets (checksummed or format-anchored to keep false positives low)
    email · payment card (Luhn) · US SSN · India PAN · AWS access key id · `sk-...` style API keys ·
    JWT · PEM private-key header

Honest scope: this is a first line of defence, not a DLP product. Phone numbers and names are *not*
detected (too noisy for regex), and the injection list catches the common, literal phrasings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9]{20,}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("pan_in", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your)\b"
        r"[^.\n]{0,30}\b(?:instructions?|prompts?|rules?|guidelines?|directives?)\b", re.I)),
    ("reveal_system_prompt", re.compile(
        r"\b(?:reveal|show|print|repeat|leak|output|tell me)\b[^.\n]{0,30}\b(?:system|hidden|initial|original)\s+"
        r"(?:prompt|instructions?|message)\b", re.I)),
    ("role_hijack", re.compile(r"\byou are now\b[^.\n]{0,40}\b(?:dan|developer mode|jailbroken|unrestricted|no rules)\b", re.I)),
    ("jailbreak_keyword", re.compile(r"\b(?:jailbreak|do anything now|dan mode)\b", re.I)),
]


def luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    return (sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])) % 10 == 0


@dataclass
class GuardrailResult:
    pii_types: list[str] = field(default_factory=list)       # distinct kinds found, in order of first appearance
    pii_count: int = 0
    injection_types: list[str] = field(default_factory=list)
    messages: Optional[list[dict]] = None                    # redacted copy (only when pii mode == redact)

    @property
    def flags(self) -> Optional[str]:
        parts = []
        if self.pii_types:
            parts.append(("pii:" if self.messages is None else "pii_redacted:") + "+".join(self.pii_types))
        if self.injection_types:
            parts.append("injection:" + "+".join(self.injection_types))
        return ",".join(parts) or None


def _redact_text(text: str, found: list[str]) -> tuple[str, int]:
    n = 0

    def sub(kind: str):
        def _r(m: re.Match) -> str:
            nonlocal n
            n += 1
            if kind not in found:
                found.append(kind)
            return f"[REDACTED:{kind.upper()}]"
        return _r

    for kind, pat in PII_PATTERNS:
        text = pat.sub(sub(kind), text)

    def card(m: re.Match) -> str:
        nonlocal n
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            n += 1
            if "card" not in found:
                found.append("card")
            return "[REDACTED:CARD]"
        return m.group(0)

    return _CARD.sub(card, text), n


def _texts(content: Any):
    """Yield (setter-key, text) for string content or text parts of a content-part array."""
    if isinstance(content, str):
        yield None, content
    elif isinstance(content, list):
        for i, part in enumerate(content):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                yield i, part["text"]


def scan(messages: list[dict], pii_mode: str = "redact", injection_mode: str = "flag") -> GuardrailResult:
    res = GuardrailResult()
    redact = pii_mode == "redact"
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        new_content = content
        for key, text in _texts(content):
            if pii_mode != "off":
                found: list[str] = []
                red, n = _redact_text(text, found)
                if n:
                    res.pii_count += n
                    for k in found:
                        if k not in res.pii_types:
                            res.pii_types.append(k)
                    if redact:
                        if key is None:
                            new_content = red
                        else:
                            new_content = list(new_content)
                            new_content[key] = {**new_content[key], "text": red}
            if injection_mode != "off" and m.get("role") == "user":
                for kind, pat in INJECTION_PATTERNS:
                    if pat.search(text) and kind not in res.injection_types:
                        res.injection_types.append(kind)
        out.append({**m, "content": new_content} if new_content is not content else m)
    if redact and res.pii_count:
        res.messages = out
    return res

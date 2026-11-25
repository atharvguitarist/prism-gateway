"""Local, offline text "embedding" used by the semantic cache.

This is a *documented local substitute* for a neural embedding model (the brief
allows normalized bag-of-words cosine and similar). It is NOT a neural model:

  1. lowercase, strip punctuation
  2. canonicalise a handful of multi-word phrases ("money back" -> refund, "two-factor" -> 2fa ...)
  3. drop stop-words and pure question scaffolding ("how do I", "what are the steps to" ...)
  4. light suffix stemming (plans -> plan, resetting -> reset, policies -> policy)
  5. map synonyms onto one concept token (purchase/bought/buy -> buy ...)
  6. weight tokens: ordinary words 1.0, generic filler ("policy", "info", "details") 0.3
  7. L2-normalise -> cosine similarity is a dot product of sparse vectors

What it captures: same-concepts-different-wording (word order, filler, a few synonyms).
What it does not: real semantics. Two prompts with the same words but opposite intent
("cancel" vs "do not cancel") are kept apart only because negations are *kept* as tokens;
anything outside the small synonym lexicon is matched literally. Swap in a real embedding
model by implementing `Embedder.embed()` (see `prism/cache.py`) -- nothing else changes.
"""
from __future__ import annotations

import math
import re
from typing import Protocol

Vector = dict[str, float]


class Embedder(Protocol):
    name: str

    def embed(self, text: str) -> Vector: ...


_PHRASES = [
    (r"\bget (?:my|the|our) money back\b|\bmoney back\b|\bpaid back\b|\bgive (?:me )?(?:my )?money back\b", " refund "),
    (r"\btwo[- ]?factor\b|\b2[- ]?fa\b|\bmulti[- ]?factor\b|\bmfa\b|\btwo[- ]?step\b", " 2fa "),
    (r"\blog[- ]?in\b|\bsign[- ]?in\b|\blog[- ]?on\b", " login "),
    (r"\blog[- ]?out\b|\bsign[- ]?out\b", " logout "),
    (r"\bset[- ]?up\b", " setup "),
    (r"\bpass[- ]?word\b", " password "),
    (r"\be[- ]?mail\b", " email "),
    (r"\bturn off\b|\bswitch off\b", " disable "),
    (r"\bturn on\b|\bswitch on\b", " enable "),
]
_PHRASES = [(re.compile(p), r) for p, r in _PHRASES]

_STOP = set("""
a an the this that these those it its i me my mine we our you your he she they them their
is are was were be been being am do does did done doing have has had having will would shall should can could may might must
of to in on at by for with from into onto about as than then so too very just also
and or if when where which who whom whose what why how whether while
please tell give show need want like know get got let help hi hello thanks thank
steps step way ways process procedure instructions instruction guide
there here some any
""".split())

# tokens that are meaningful negations / modifiers and must survive stop-word removal
_KEEP = {"not", "no", "never", "without", "dont", "cant", "cannot", "wont", "only"}

_SYN_GROUPS = [
    ("refund", "refunds", "refunded", "reimburse", "reimbursement", "reimbursed", "chargeback"),
    ("buy", "bought", "purchase", "purchased", "buying", "order", "ordered"),
    ("price", "pricing", "cost", "costs", "fee", "fees", "charge", "charges"),
    ("cancel", "cancellation", "terminate", "unsubscribe"),
    ("delete", "remove", "erase", "deletion"),
    ("change", "modify", "update", "edit", "alter"),
    ("reset", "recover", "restore", "forgot", "forget", "forgotten"),
    ("start", "begin", "launch"),
    ("fix", "repair", "resolve", "solve", "troubleshoot"),
    ("fast", "quick", "quickly", "rapid"),
    ("error", "bug", "failure", "fail", "failing", "crash"),
    ("annual", "yearly", "year"),
    ("monthly", "month"),
    ("phone", "mobile", "cellphone", "smartphone"),
    ("contact", "reach"),
    ("login", "authenticate", "authentication"),
    ("invoice", "receipt", "bill", "billing"),
]
_SYN = {w: g[0] for g in _SYN_GROUPS for w in g}

_GENERIC = {"buy", "policy", "information", "info", "details", "detail", "issue", "problem", "question", "thing", "stuff"}

_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            base = w[: -len(suf)]
            if len(base) > 2 and base[-1] == base[-2] and base[-1] not in "ls":
                base = base[:-1]  # "running" -> "run"
            return base
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("es") and len(w) > 4 and w[-3] in "sxz":
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


class LexicalConceptEmbedder:
    name = "lexical-concept-v1"

    def tokens(self, text: str) -> list[str]:
        t = text.lower().replace("'", "")  # don't -> dont
        for rx, repl in _PHRASES:
            t = rx.sub(repl, t)
        out = []
        for w in _TOKEN.findall(t):
            if w in _STOP and w not in _KEEP:
                continue
            w = _SYN.get(w) or _SYN.get(_stem(w)) or _stem(w)
            out.append(_SYN.get(w, w))
        return out

    def embed(self, text: str) -> Vector:
        vec: Vector = {}
        for tok in self.tokens(text):
            vec[tok] = max(vec.get(tok, 0.0), 0.3 if tok in _GENERIC else 1.0)
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {k: v / norm for k, v in vec.items()} if norm else {}


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two L2-normalised sparse vectors."""
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())

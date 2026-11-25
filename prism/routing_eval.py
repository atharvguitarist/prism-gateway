"""One-command routing eval.

    python -m prism.routing_eval                      # provided 20-case set, human table
    python -m prism.routing_eval --json               # machine-readable report
    python -m prism.routing_eval --file data/routing_eval_heldout.jsonl

Also exposed over HTTP as GET /admin/routing/eval. Only classification is run (no upstream call).
`expected_tier` is read here for scoring and is never visible to the classifier.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .smart_router import classify


def length_only_baseline(prompt: str, cutoff_words: int = 25) -> str:
    return "smart" if len(prompt.split()) > cutoff_words else "fast"


def run_eval(path: str | Path) -> dict[str, Any]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    out, correct, base_correct = [], 0, 0
    for c in cases:
        d = classify(c["prompt"])
        ok = d.tier == c["expected_tier"]
        correct += ok
        base = length_only_baseline(c["prompt"])
        base_correct += base == c["expected_tier"]
        out.append({"id": c["id"], "expected": c["expected_tier"], "actual": d.tier, "correct": ok,
                    "score": d.score, "reason": d.reason, "note": c.get("note", ""),
                    "length_baseline": base})
    n = len(cases)
    return {
        "file": str(path), "total": n, "correct": correct,
        "accuracy": round(correct / n, 4) if n else 0.0,
        "length_only_baseline_accuracy": round(base_correct / n, 4) if n else 0.0,
        "method": "engineered-signal scorer over instruction sentences (prism/smart_router.py)",
        "cases": out,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/routing_eval.jsonl")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--misses-only", action="store_true")
    a = ap.parse_args()
    rep = run_eval(a.file)
    if a.json:
        print(json.dumps(rep, indent=2))
        return
    print(f"Routing eval: {rep['file']}")
    for c in rep["cases"]:
        if a.misses_only and c["correct"]:
            continue
        mark = "OK  " if c["correct"] else "MISS"
        print(f"  {mark} {c['id']:<10} expected={c['expected']:<5} actual={c['actual']:<5} "
              f"score={c['score']:+.1f}  {c['note']}")
        if not c["correct"] or a.misses_only:
            print(f"        why: {c['reason']}")
    print(f"\nAccuracy: {rep['correct']}/{rep['total']} = {rep['accuracy']:.0%}   "
          f"(length-only baseline: {rep['length_only_baseline_accuracy']:.0%})")


if __name__ == "__main__":
    main()

import json

from prism.routing_eval import run_eval
from prism.smart_router import classify

from conftest import ROOT


def test_provided_eval_set_beats_length_baseline_by_a_wide_margin():
    rep = run_eval(ROOT / "data" / "routing_eval.jsonl")
    assert rep["total"] == 20
    assert rep["accuracy"] >= 0.85
    assert rep["accuracy"] > rep["length_only_baseline_accuracy"] + 0.3


def test_heldout_set_generalises():
    assert run_eval(ROOT / "data" / "routing_eval_heldout.jsonl")["accuracy"] >= 0.85


def test_the_traps():
    assert classify("Prove that the square root of 2 is irrational.").tier == "smart"      # short but hard
    roster = ("Here is our on-call roster: " + ", ".join(f"Day{i} - Person{i}" for i in range(14))
              + ". Who is on call this Thursday?")
    assert classify(roster).tier == "fast"                                                    # long but trivial


def test_router_never_reads_labels():
    import inspect, prism.smart_router as m
    assert "expected_tier" not in inspect.getsource(m)


def test_decision_carries_a_reason():
    d = classify("Design a database schema for billing and explain the trade-offs.")
    assert d.tier == "smart" and "design" in d.reason and d.score > 2

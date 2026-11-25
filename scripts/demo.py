#!/usr/bin/env python3
"""Walk through the Prism demo flow end to end (stdlib only; Python 3.9+).

    python3 scripts/demo.py --pause            # for the video: press Enter between steps
    python3 scripts/demo.py                    # run straight through (also used as a verification drill)

Assumes the stack is up (docker compose up, or the three local processes from the README):
gateway :8080, mock providers alpha :9001 and beta :9002.

Every step ASSERTS what it shows, so a green run is evidence, not just a printout.
Exit code 1 if any assertion fails.

The budget-demo key and the free-tier key are consumed by this flow (a tiny budget / a 10 req/min limit
is the point). To replay on a clean slate:  docker compose down -v && docker compose up -d   (or delete prism.sqlite3).
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
SEARCH, RESEARCH, FREE, BUDGET = ("prism-sk-search-1a2b3c", "prism-sk-research-4d5e6f",
                                  "prism-sk-free-7g8h9i", "prism-sk-budget-demo-0j1k2l")
FAILS = []
C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "c": "\033[36m", "x": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def say(msg="", c=""):
    print(f"{C.get(c, '')}{msg}{C['x']}")


def step(n, title, args):
    if args.pause and n > 1:
        input(f"\n{C['d']}   [Enter] for step {n} …{C['x']}")
    say(f"\n━━ Step {n}: {title} " + "━" * max(2, 70 - len(title)), "b")


def check(cond, what):
    say(f"   {'✔' if cond else '✘'} {what}", "g" if cond else "r")
    if not cond:
        FAILS.append(what)
    return cond


def http_json(method, url, key=None, body=None, admin=None, raw=False):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if admin:
        h["X-Admin-Token"] = admin
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, json.loads(data or b"{}")
        except json.JSONDecodeError:
            return e.code, {}, data.decode(errors="replace")


def chat(gw, key, content, model="fast", **extra):
    return http_json("POST", gw + "/v1/chat/completions", key,
                     {"model": model, "messages": [{"role": "user", "content": content}], **extra})


def show_headers(h, extra=()):
    for k in ("x-prism-provider", "x-prism-cache", "x-prism-fallback", "x-prism-cost-usd", *extra):
        if k in h:
            say(f"      {k}: {h[k]}", "c")


def stream(gw, key, content, model="fast"):
    u = urllib.parse.urlparse(gw)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    conn.request("POST", "/v1/chat/completions",
                 json.dumps({"model": model, "stream": True, "messages": [{"role": "user", "content": content}]}),
                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    r = conn.getresponse()
    headers = {k.lower(): v for k, v in r.getheaders()}
    t0, events = time.time(), []
    buf = b""
    while True:
        chunk = r.fp.readline()
        if not chunk:
            break
        line = chunk.decode().strip()
        if line.startswith("data:"):
            events.append((time.time() - t0, line[5:].strip()))
    conn.close()
    return r.status, headers, events


def mock(port, cfg):
    return http_json("POST", f"http://localhost:{port}/admin/config", body=cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--alpha", type=int, default=9001)
    ap.add_argument("--beta", type=int, default=9002)
    ap.add_argument("--admin-token", default=os.environ.get("PRISM_ADMIN_TOKEN", "prism-admin-demo"))
    ap.add_argument("--pause", action="store_true", help="wait for Enter between steps (for recording)")
    ap.add_argument("--no-salt", action="store_true", help="use the exact sample prompts (needs a fresh DB)")
    args = ap.parse_args()
    gw, adm = args.gateway, args.admin_token
    salt = "" if args.no_salt else f"({uuid.uuid4().hex[:6]}) "
    mock(args.alpha, {"mode": "ok", "fail_rate": 0, "latency_ms": 0})
    mock(args.beta, {"mode": "ok", "fail_rate": 0, "latency_ms": 0})
    samples = {json.loads(l)["id"]: json.loads(l) for l in open(os.path.join(DATA, "sample_requests.jsonl")) if l.strip()}
    prompt = lambda rid: salt + samples[rid]["body"]["messages"][-1]["content"]

    step(1, "Both mock providers up, gateway healthy", args)
    s, _, body = http_json("GET", gw + "/health?deep=1")
    say(f"      GET /health?deep=1 → {json.dumps(body)}", "c")
    check(s == 200 and body.get("providers") == {"alpha": "up", "beta": "up"}, "gateway healthy; alpha and beta reachable")

    step(2, "Non-streaming request (search team key → `fast` alias)", args)
    s, h, b = chat(gw, SEARCH, salt + "What is a message queue and when should I use one?")
    say(f"      HTTP {s}  →  {b['choices'][0]['message']['content'][:80]}…")
    show_headers(h)
    check(s == 200 and h["x-prism-cache"] == "miss" and float(h["x-prism-cost-usd"]) > 0,
          "served by a provider, cache miss, cost metered from provider-reported usage")

    step(3, "Streaming request — tokens arrive one by one", args)
    s, h, ev = stream(gw, SEARCH, salt + "Explain how server-sent events work.")
    show_headers(h)
    for t, d in ev[:7]:
        try:
            tok = json.loads(d)["choices"][0]["delta"].get("content", "")
        except Exception:
            tok = d
        say(f"      +{t * 1000:5.0f} ms  {tok!r}")
    say(f"      … {len(ev)} events in {ev[-1][0] * 1000:.0f} ms; last event: {ev[-1][1]}")
    gaps = [ev[i + 1][0] - ev[i][0] for i in range(1, min(len(ev) - 2, 10))]
    check(ev[-1][1] == "[DONE]" and ev[-1][0] - ev[1][0] > 0.15 and sum(g > 0.005 for g in gaps) >= len(gaps) * 0.6,
          "stream is progressive (not buffered) and terminates with [DONE]")

    step(4, "Semantic cache: repeat, paraphrase, and a near-miss (pair A)", args)
    a1, a2, a3 = prompt("req_cache_a1"), prompt("req_cache_a2"), prompt("req_cache_a3")
    out = []
    for label, p in (("ask        ", a1), ("repeat     ", a1), ("paraphrase ", a2), ("near-miss  ", a3)):
        s, h, b = chat(gw, SEARCH, p)
        sim = h.get("x-prism-cache-similarity", "")
        say(f"      {label} x-prism-cache: {h['x-prism-cache']:<4}  cost: {h['x-prism-cost-usd']:<10} {('similarity ' + sim) if sim else ''}   «{p[len(salt):][:55]}»", "c")
        out.append(h["x-prism-cache"])
    check(out == ["miss", "hit", "hit", "miss"], "repeat and paraphrase hit at $0; different-intent prompt (2FA reset) is NOT served the password answer")
    say("      tenant isolation: the same prompt from a *different* team must miss", "d")
    s, _, k = http_json("POST", gw + "/admin/keys", admin=adm, body={
        "team": "isolation-demo", "monthly_budget_usd": 5, "rate_limit": {"requests_per_minute": 60},
        "model_allowlist": ["fast"], "semantic_cache": {"enabled": True, "similarity_threshold": 0.9}})
    s, h, _ = chat(gw, k["virtual_key"], a1)
    say(f"      other team, identical prompt → x-prism-cache: {h['x-prism-cache']}", "c")
    check(h["x-prism-cache"] == "miss", "cache is scoped per key — no cross-tenant leak")

    step(5, "Smart routing (`auto`, research key): short-but-hard vs long-but-trivial", args)
    for rid, expect in (("route_011", "smart"), ("route_006", "fast")):
        case = next(json.loads(l) for l in open(os.path.join(DATA, "routing_eval.jsonl")) if json.loads(l)["id"] == rid)
        s, h, _ = chat(gw, RESEARCH, case["prompt"], model="auto")
        say(f"      {rid} ({len(case['prompt'].split())} words) «{case['prompt'][:60]}…»", "b")
        show_headers(h, ("x-prism-route", "x-prism-route-reason"))
        check(h["x-prism-route"] == f"auto:{expect}", f"{rid} routed to `{expect}`")
    s, _, rep = http_json("GET", gw + "/admin/routing/eval", admin=adm)
    say(f"      routing eval over data/routing_eval.jsonl: {rep['correct']}/{rep['total']} = {rep['accuracy']:.0%} "
        f"(length-only baseline: {rep['length_only_baseline_accuracy']:.0%})", "c")
    check(rep["accuracy"] >= 0.8 and rep["accuracy"] > rep["length_only_baseline_accuracy"], "router beats the length-only baseline")

    step(6, "Rate limiting: burst past the free-tier key's 10 req/min", args)
    r = subprocess.run([sys.executable, os.path.join(HERE, "load_test.py"), "--url", gw, "--key", FREE, "--model", "fast",
                        "--requests", "30", "--concurrency", "10", "--rpm-limit", "10"], capture_output=True, text=True)
    print("".join("      " + l + "\n" for l in r.stdout.splitlines() if l.strip()))
    s, h, b = chat(gw, FREE, "one more request")
    say(f"      next request → HTTP {s}  {json.dumps(b['error'])}   retry-after: {h.get('retry-after')}s", "c")
    check(r.returncode == 0 and "Over-admission check OK" in r.stdout and s == 429 and b["error"]["type"] == "rate_limit_exceeded",
          "zero over-admission; the rest are clean 429 rate_limit_exceeded")
    s, _, u = http_json("GET", gw + f"/admin/usage?key={FREE}", admin=adm)
    say(f"      usage API for this key: requests={u['requests']} prompt={u['prompt_tokens']} completion={u['completion_tokens']} cost=${u['cost_usd']}", "c")

    step(7, "Budget: the tiny-budget key gets one request, then is rejected", args)
    p = salt + "What is a message queue and when should I use one?"
    s1, h1, b1 = chat(gw, BUDGET, p)
    s2, h2, b2 = chat(gw, BUDGET, p + " Please elaborate.")
    say(f"      1st → HTTP {s1}, cost {h1.get('x-prism-cost-usd')}", "c")
    say(f"      2nd → HTTP {s2}  {json.dumps(b2.get('error'))}", "c")
    if s1 == 402:
        say("      (budget already consumed by an earlier run; reset the DB to replay the first half)", "y")
    check((s1 == 200 or s1 == 402) and s2 == 402 and b2["error"]["type"] == "budget_exceeded", "second request rejected with budget_exceeded")

    step(8, "Failure drill: take provider alpha down, live", args)
    mock(args.alpha, {"mode": "down"})
    t0 = time.time()
    s, h, b = chat(gw, SEARCH, salt + "Failover drill: what is idempotency?")
    show_headers(h)
    check(s == 200 and h["x-prism-fallback"] == "true" and h["x-prism-provider"].startswith("beta/"),
          f"alpha down → served by beta with x-prism-fallback: true ({time.time() - t0:.1f}s incl. retries)")
    s, h, _ = chat(gw, SEARCH, salt + "Failover drill: second request, circuit open")
    check(s == 200 and h.get("x-prism-circuit-skipped") == "alpha",
          "circuit breaker open → alpha skipped entirely (no retry/timeout tax on later requests)")
    _, _, gcfg = http_json("GET", gw + "/admin/config", admin=adm)
    cooldown = float(gcfg.get("circuit_breaker", {}).get("cooldown_s", 5)) + 0.5

    def wait_cooldown(why):
        say(f"      …waiting {cooldown:.1f}s for the circuit-breaker cooldown ({why})", "d")
        time.sleep(cooldown)

    wait_cooldown("next request is the half-open probe")
    say("      …now alpha is merely SLOW (3 s) — the 2 s upstream timeout must keep the client from hanging", "d")
    mock(args.alpha, {"mode": "ok", "latency_ms": 3000})
    t0 = time.time()
    s, h, _ = chat(gw, SEARCH, salt + "Failover drill: what is a timeout?")
    dt = time.time() - t0
    say(f"      answered in {dt:.1f}s by {h['x-prism-provider']} (fallback={h['x-prism-fallback']})", "c")
    check(s == 200 and dt < 3.0 and h["x-prism-fallback"] == "true", "slow provider → timeout fires → fallback; no hang")
    wait_cooldown("let alpha be probed again")
    say("      …alpha flaky (30% errors): 20 requests", "d")
    mock(args.alpha, {"mode": "ok", "latency_ms": 0, "fail_rate": 0.3})
    codes = [chat(gw, RESEARCH, salt + f"flaky drill {i}")[0] for i in range(20)]
    check(all(c == 200 for c in codes), f"retries absorb a 30% error rate ({codes.count(200)}/20 succeeded)")
    mock(args.alpha, {"mode": "ok", "latency_ms": 0, "fail_rate": 0})
    wait_cooldown("a probe closes the circuit")
    s, h, _ = chat(gw, SEARCH, salt + "Failover drill: alpha restored?")
    check(h["x-prism-provider"].startswith("alpha/") and h["x-prism-fallback"] == "false", "alpha restored → traffic returns to the primary")

    step(9, "Ops console", args)
    s, _, c = http_json("GET", gw + "/admin/cache/stats", admin=adm)
    say(f"      cache stats: {c['hits']} hits / {c['lookups']} lookups = {c['hit_rate']:.0%}, saved ${c['cost_saved_usd']:.6f}", "c")
    say(f"      open  {gw}/console   (admin token: {adm})", "b")
    check(c["hits"] >= 2, "cache hits are visible in the cache stats")

    step(10, "Provided smoke test", args)
    r = subprocess.run([sys.executable, os.path.join(HERE, "smoke_test.py"), "--url", gw, "--key", SEARCH, "--model", "fast"],
                       capture_output=True, text=True)
    print("".join("      " + l + "\n" for l in r.stdout.splitlines() if l.strip()))
    check(r.returncode == 0, "smoke test passes")

    say("\n" + ("═" * 74), "b")
    say(f"{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' CHECK(S) FAILED'}", "g" if not FAILS else "r")
    for f in FAILS:
        say(f"  ✘ {f}", "r")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

from prism.cache import SemanticCache, context_hash, is_cacheable_request, is_refusal
from prism.embedding import LexicalConceptEmbedder, cosine
from prism.store import Store

E = LexicalConceptEmbedder()
msg = lambda t: [{"role": "user", "content": t}]


def sim(a, b): return cosine(E.embed(a), E.embed(b))


def test_pair_a_paraphrase_is_near_identical_and_near_miss_is_far():
    a1 = "How do I reset my password on the dashboard?"
    assert sim(a1, "What are the steps to reset my dashboard password?") >= 0.95
    assert sim(a1, "How do I reset my two-factor authentication on the dashboard?") < 0.7   # req_cache_a3


def test_pair_b_paraphrase_clears_the_search_threshold_and_unrelated_does_not():
    assert sim("What is the refund policy for annual plans?", "If I bought an annual plan, can I get my money back?") >= 0.92
    assert sim("How do I reset my password on the dashboard?", "Compare TCP and UDP for game servers.") < 0.3


def test_negation_is_not_ignored():
    assert sim("Can I cancel my subscription?", "Can I not cancel my subscription?") < 0.9


def make_cache(**kw):
    return SemanticCache(Store(":memory:"), **kw)


def test_hit_on_paraphrase_miss_on_other_intent():
    c = make_cache()
    c.put("k1", "fast", msg("How do I reset my password on the dashboard?"), {"content": "steps", "model": "m"}, "alpha/alpha-small", 100)
    assert c.lookup("k1", "fast", msg("What are the steps to reset my dashboard password?"), 0.92)
    assert not c.lookup("k1", "fast", msg("How do I reset my two-factor authentication on the dashboard?"), 0.92)


def test_cache_is_isolated_between_tenants_and_scopes():
    c = make_cache()
    c.put("team-a", "fast", msg("What is our refund policy?"), {"content": "A's secret answer"}, "x", 0)
    assert c.lookup("team-a", "fast", msg("What is our refund policy?"), 0.9)
    assert c.lookup("team-b", "fast", msg("What is our refund policy?"), 0.0) is None   # even at threshold 0
    assert c.lookup("team-a", "smart", msg("What is our refund policy?"), 0.0) is None


def test_multiturn_context_must_match_exactly():
    c = make_cache()
    convo = [{"role": "user", "content": "What is connection pooling?"},
             {"role": "assistant", "content": "It reuses connections."},
             {"role": "user", "content": "What happens when the pool is exhausted?"}]
    c.put("k", "fast", convo, {"content": "requests queue up"}, "x", 0)
    assert c.lookup("k", "fast", convo, 0.9)
    other = [{"role": "user", "content": "What is a thread pool?"}, convo[1], convo[2]]
    assert c.lookup("k", "fast", other, 0.9) is None    # same last message, different conversation
    assert context_hash(msg("hi")) == ""


def test_ttl_and_lru_cap():
    t = [1000.0]
    c = make_cache(ttl_seconds=60, max_entries_per_key=2, clock=lambda: t[0])
    for i, p in enumerate(["alpha topic one", "bravo topic two", "charlie topic three"]):
        c.put("k", "fast", msg(p), {"content": str(i)}, "x", 0); t[0] += 1
    assert c.size("k") == 2 and c.lookup("k", "fast", msg("alpha topic one"), 0.9) is None   # oldest evicted
    t[0] += 120
    assert c.lookup("k", "fast", msg("charlie topic three"), 0.9) is None                   # expired


def test_what_is_not_cacheable():
    assert not is_cacheable_request({}, "What is the current status of the payments service?")[0]
    assert not is_cacheable_request({"tools": [{"type": "function"}]}, "hello")[0]
    assert not is_cacheable_request({"n": 3}, "hello")[0]
    assert is_cacheable_request({}, "Explain how a hash map works")[0]
    assert is_refusal("I'm sorry, but I can't help with that.") and not is_refusal("Sorry for the delay: here you go")

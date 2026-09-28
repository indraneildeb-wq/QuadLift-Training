from oceanbridge.cache.embeddings import LocalHashEmbedder
from oceanbridge.cache.semantic_cache import CacheQuery, SemanticCache


def q(weight=18_000, origin="CNSHA", dest="NLRTM", text=None):
    guard = {"origin": origin, "destination": dest, "mode": "sea", "weight_kg": weight, "volume_cbm": 60}
    return CacheQuery(
        params={**guard, "exact": weight},
        text=text or f"Reroute sea shipment from {origin} to {dest}, about {round(weight / 1000)} tonnes, port strike",
        guard=guard, touchpoints=[origin, dest, "SUEZ"],
    )


def test_l1_exact_hit(fresh_db):
    c = SemanticCache("t1", LocalHashEmbedder())
    assert c.get(q()) is None
    c.put(q(), {"answer": 1})
    hit = c.get(q())
    assert hit.tier == "L1-exact" and hit.payload == {"answer": 1}


def test_l2_semantic_hit_within_guard(fresh_db):
    c = SemanticCache("t2", LocalHashEmbedder())
    c.put(q(18_000), {"answer": 1})
    hit = c.get(q(18_400))  # 2% heavier, same lane: different exact key, same meaning
    assert hit is not None and hit.tier == "L2-semantic" and hit.similarity >= 0.92


def test_structural_guard_blocks_wrong_lane_and_size(fresh_db):
    c = SemanticCache("t3", LocalHashEmbedder())
    c.put(q(18_000), {"answer": 1})
    # Identical wording but different destination port: must NOT reuse the route.
    assert c.get(q(18_000, dest="DEHAM", text=q(18_000).text)) is None
    # Same lane but 40% heavier: outside tolerance.
    assert c.get(q(25_200)) is None


def test_l3_persistent_survives_restart(fresh_db):
    SemanticCache("t4", LocalHashEmbedder()).put(q(), {"answer": 42})
    reborn = SemanticCache("t4", LocalHashEmbedder())
    hit = reborn.get(q())
    assert hit.tier == "L3-persistent" and hit.payload["answer"] == 42
    assert reborn.get(q()).tier == "L1-exact"  # promoted


def test_invalidation_on_disruption_location(fresh_db):
    c = SemanticCache("t5", LocalHashEmbedder())
    c.put(q(), {"answer": 1})
    assert c.invalidate_locations({"SUEZ"}) == 1
    assert c.get(q()) is None
    assert SemanticCache("t5", LocalHashEmbedder()).get(q()) is None  # gone from L3 too


def test_embedder_similarity_ordering():
    e = LocalHashEmbedder()
    a = e.embed("Reroute sea shipment from Shanghai to Rotterdam, port strike")
    b = e.embed("Reroute sea shipment from Shanghai to Rotterdam port strike")
    z = e.embed("Air freight quote Los Angeles to New York")
    assert float(a @ b) > float(a @ z)

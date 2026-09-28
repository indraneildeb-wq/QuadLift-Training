# Semantic cache: how L1, L2 and L3 are maintained

The rerouting system caches **route decisions** and **freight quotes** in three tiers, so a repeated or similar question is answered without calling the LLM or re-running the tariff. All tiers live in one class, `SemanticCache`, in [cache/semantic_cache.py](../packages/core/src/oceanbridge/cache/semantic_cache.py). Tuning values are under `cache:` in [config/settings.yaml](../config/settings.yaml).

Code lives in the workspace packages under `packages/` (see the README's *Code layout*). A module path such as `core/db.py` means `src/oceanbridge/core/db.py` inside the package that owns it.

| Tier | Match | Where it lives | Survives a restart | Shared between processes |
|---|---|---|---|---|
| **L1** exact | Identical query (same key) | In-process dictionary (`cachetools.TTLCache`) | No | No |
| **L3** persistent | Identical query (same key) | SQLite table `cache_entries` | Yes | Yes |
| **L2** semantic | Similar query (embedding similarity ≥ 0.92) **and** structural guard | In-process NumPy matrix, loaded from L3 | Yes (reloaded from L3) | No |

**Lookup order is L1 → L3 → L2.** Both exact tiers are checked before the approximate one, because an exact answer always beats a similar one.

---

## 1. What is cached

Each namespace is a separate `SemanticCache` instance with its own L1 and L2, created on first use by `get_cache(namespace)` ([L226](../packages/core/src/oceanbridge/cache/semantic_cache.py#L226)).

| Namespace | What is stored | Used in | Structural guard (all must match) |
|---|---|---|---|
| `route_decisions` | The optimizer's chosen route **strategy**: a route signature plus the ranking and rationale. It is re-priced for the actual shipment on every hit, so a cached answer never reuses stale prices | [agents/route_optimizer/optimizer.py](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/optimizer.py#L74), query built by `route_decision_query()` ([L30](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/optimizer.py#L30)) | origin, destination, mode, status, current route, disruption fingerprint, value band, delivery-slack band; weight and volume within ±10% |
| `freight` | A tariff quote: cost, transit days, distance, CO₂ | [core/service.py](../packages/core/src/oceanbridge/core/service.py#L69) `_freight_lookup()`, query built by `_freight_query()` ([L48](../packages/core/src/oceanbridge/core/service.py#L48)) | origin, destination, mode, carrier, forced route (`via`), chargeable units (TEU or kg) |

Every query (`CacheQuery`) carries four things:

| Field | Purpose |
|---|---|
| `params` | Exact parameters. `namespace:` + SHA-256 of these is the **key** used by L1 and L3 |
| `text` | A natural-language rendering of the query that is embedded for L2, e.g. *"Reroute a booked sea shipment from Shenzhen (CNSZX) to Rotterdam (NLRTM), about 18 tonnes, standard value band, moderate delivery window. Disruptions: …"* |
| `guard` | The structural fields a semantic hit must match (table above) |
| `touchpoints` | Ports and chokepoints the route passes through, used for invalidation |

---

## 2. L1: exact match, in memory

| Aspect | How it works | Code |
|---|---|---|
| Storage | `cachetools.TTLCache` dictionary inside the process | [L79](../packages/core/src/oceanbridge/cache/semantic_cache.py#L79) |
| Key | `namespace:` + SHA-256 of the query's `params` | `CacheQuery.key()` |
| Size limit | `l1_max_entries: 2048`. When full, the **least recently used** entry is dropped | settings.yaml L30 |
| Expiry | `l1_ttl_seconds: 900` (15 min) after the entry was stored | settings.yaml L31 |
| Filled by | `put()` stores every new answer | [L173](../packages/core/src/oceanbridge/cache/semantic_cache.py#L173) |
| Promotion | An L3 or L2 hit copies the answer into L1, so the next identical query is instant | [L138](../packages/core/src/oceanbridge/cache/semantic_cache.py#L138), [L156](../packages/core/src/oceanbridge/cache/semantic_cache.py#L156) |
| Removed by | Expiry, LRU eviction, disruption invalidation, `clear()` | [L200](../packages/core/src/oceanbridge/cache/semantic_cache.py#L200), [L206](../packages/core/src/oceanbridge/cache/semantic_cache.py#L206) |

---

## 3. L2: semantic match, in memory

| Aspect | How it works | Code |
|---|---|---|
| Storage | Three parallel structures: `_l2_keys` (keys), `_l2_vecs` (NumPy matrix, one normalised vector per row), `_l2_meta` (answer, guard, expiry time and route points per key) | [L80–82](../packages/core/src/oceanbridge/cache/semantic_cache.py#L80) |
| Vector | Made from `text` by the embedder: `text-embedding-3-small` with an OpenAI key, `LocalHashEmbedder` without one (`embedder: auto`) | [cache/embeddings.py](../packages/core/src/oceanbridge/cache/embeddings.py) |
| Loading at start-up | `_hydrate()` runs on the process's first `get()` or `put()` and loads every **unexpired** L3 row of that namespace into L2 | [L88](../packages/core/src/oceanbridge/cache/semantic_cache.py#L88) |
| Similarity | `_l2_vecs @ vector`, which is cosine similarity because all vectors are normalised | [L144](../packages/core/src/oceanbridge/cache/semantic_cache.py#L144) |
| Candidates | The top 10 by similarity, stopping at the first below `l2_similarity_threshold: 0.92` | [L146–149](../packages/core/src/oceanbridge/cache/semantic_cache.py#L146) |
| Expired entries | Skipped at lookup, but **not deleted** from memory | [L152](../packages/core/src/oceanbridge/cache/semantic_cache.py#L152) |
| Structural guard | `guard_matches()`: every field equal, except weight and volume, which may differ by `weight_tolerance: 0.10` (±10%). Stops a similar-sounding query for another lane from reusing the wrong route | [L57](../packages/core/src/oceanbridge/cache/semantic_cache.py#L57), [L154](../packages/core/src/oceanbridge/cache/semantic_cache.py#L154) |
| Size limit | `l2_max_entries: 5000`. When exceeded, the **oldest** entry is removed first (first in, first out) | [L110](../packages/core/src/oceanbridge/cache/semantic_cache.py#L110) |
| Replacing an entry | Storing a key that is already present removes its old vector first, so there are no duplicates | [L103](../packages/core/src/oceanbridge/cache/semantic_cache.py#L103) |
| Changed embedder | Vectors of a different length are skipped, e.g. after switching from local to OpenAI embeddings | [L105](../packages/core/src/oceanbridge/cache/semantic_cache.py#L105) |
| Removed by | Oldest-first eviction, disruption invalidation, `clear()` | [L198](../packages/core/src/oceanbridge/cache/semantic_cache.py#L198), [L207](../packages/core/src/oceanbridge/cache/semantic_cache.py#L207) |

---

## 4. L3: persistent

| Aspect | How it works |
|---|---|
| Storage | SQLite table `cache_entries`: key, namespace, text, embedding (bytes), payload, guard, touchpoints, created_at, expires_at, hits. See [database.md](database.md) |
| Lookup | Read by primary key; an unexpired row counts as a hit, increments `hits` and is promoted to L1 ([L133–139](../packages/core/src/oceanbridge/cache/semantic_cache.py#L133)) |
| Role | Keeps answers across restarts, is shared by the API and the MCP server process, and is the source `_hydrate()` rebuilds L2 from |

---

## 5. Life of an entry

### Lookup: `get()` ([L123](../packages/core/src/oceanbridge/cache/semantic_cache.py#L123))

```
query ──► L1 exact key? ──yes──► return (tier "L1-exact")
             │ no
             ▼
          L3 exact key, not expired? ──yes──► copy to L1 ──► return ("L3-persistent")
             │ no
             ▼
          L2: embed text, top-10 by cosine ≥ 0.92, not expired, guard matches?
             ──yes──► copy to L1 ──► return ("L2-semantic", similarity)
             │ no
             ▼
          miss ──► caller computes the answer (LLM decision or tariff) and calls put()
```

### Store: `put()` ([L165](../packages/core/src/oceanbridge/cache/semantic_cache.py#L165))

One call writes **all three tiers**:

| Tier | What is written | Expiry |
|---|---|---|
| L1 | key → answer | 15 min (`l1_ttl_seconds`) |
| L2 | vector + answer, guard, expiry, route points | same expiry as L3 (below) |
| L3 | full row in `cache_entries` | 24 h (`l3_ttl_seconds: 86400`), or **5 min** (`disrupted_ttl_seconds: 300`) when the route touches an active disruption |

### Invalidation: `invalidate_locations()` ([L185](../packages/core/src/oceanbridge/cache/semantic_cache.py#L185))

When the flow's `detect_disruptions` step finds a **new** disruption, it calls `invalidate_locations()` on the `route_decisions` cache with the disruption's location ([flow/reroute_flow.py L80](../packages/app/src/oceanbridge/flow/reroute_flow.py#L80)). Every entry whose `touchpoints` include that port or chokepoint is removed from **L1, L2 and L3** at once, and the number removed is logged in the run trace.

The `freight` namespace is not invalidated: a tariff quote doesn't depend on disruptions, and disruption surcharges and delays are added afterwards by the routing engine.

### Clearing: `clear()` ([L204](../packages/core/src/oceanbridge/cache/semantic_cache.py#L204))

Empties L1, L2 and the namespace's L3 rows, and resets the counters. `POST /admin/seed` (the UI's **Reset demo data**) calls it for every cache in the API process ([api/main.py L76](../packages/app/src/oceanbridge/api/main.py#L76)).

---

## 6. Processes, threads and metrics

| Topic | Behaviour |
|---|---|
| Per process | L1 and L2 live inside each process, so the REST API and the MCP server subprocess keep their own. L3 is shared through SQLite |
| Threads | Every tier operation runs under one `threading.RLock` ([L78](../packages/core/src/oceanbridge/cache/semantic_cache.py#L78)), because the API serves requests on several threads |
| Metrics | Counters for L1/L2/L3 hits, misses, stores, invalidations and tokens saved feed `snapshot()` ([L213](../packages/core/src/oceanbridge/cache/semantic_cache.py#L213)), shown on the Metrics tab and by `GET /metrics` |
| Tokens saved | When an LLM decision is stored, its token count is kept in the answer (`_tokens`); every later hit adds that count to `tokens_saved` |

---

## 7. Configuration

All settings are in [config/settings.yaml](../config/settings.yaml) and can be overridden with environment variables such as `OB_CACHE__L1_TTL_SECONDS=600`. Defaults are in [config.py](../packages/core/src/oceanbridge/config.py#L46) (`CacheSettings`).

| Setting | Default | Controls |
|---|---|---|
| `l1_max_entries` | 2048 | L1 size before LRU eviction |
| `l1_ttl_seconds` | 900 | L1 expiry |
| `l2_similarity_threshold` | 0.92 | Minimum cosine similarity for an L2 hit |
| `l2_max_entries` | 5000 | L2 size before oldest-first eviction |
| `l3_ttl_seconds` | 86400 | L2 and L3 expiry for normal entries |
| `disrupted_ttl_seconds` | 300 | L2 and L3 expiry when the route touches an active disruption |
| `weight_tolerance` | 0.10 | Allowed weight and volume difference in the structural guard |
| `embedder` | auto | `openai`, `local`, or `auto` (OpenAI when a key is set) |

---

## 8. Seeing it work

| Action | Expected tier |
|---|---|
| Reroute Proposals tab → **Optimise** a shipment | `miss` |
| Click **Optimise** again | `L1-exact` |
| Change the weight by about +4% → **Optimise** | `L2-semantic` |
| Change the weight by +50% → **Optimise** | `miss` (guard rejects it) |
| Restart the API, then **Optimise** the first query again | `L3-persistent` |
| Rejected shipment on the next pipeline run | `L1-exact` in the run trace; no LLM call |

These cases are covered by `packages/core/tests/test_semantic_cache.py` and `packages/app/tests/test_pipeline.py`.

---

## 9. Known issue: L1 ignores the short disruption expiry

L1 keeps every entry for **15 minutes**, even when the route touches a disruption and L2/L3 expire the same entry after **5 minutes**. For up to 10 minutes after the 5-minute window, an exact repeat of that query can still be served from L1.

- **Covered:** a **new** disruption on the route. Invalidation removes the entry from all three tiers immediately.
- **Not covered:** a disruption that has **ended**. The old decision can stay in L1 for up to 10 extra minutes.

**Fix (not yet applied):** store the expiry time with each L1 entry and check it on lookup, or keep disrupted entries in a second L1 with a 5-minute TTL.

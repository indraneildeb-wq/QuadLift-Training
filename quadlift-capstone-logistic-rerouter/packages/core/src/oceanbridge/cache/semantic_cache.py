"""Multi-tier semantic cache for route-calculation queries.

  L1  exact      in-process TTL LRU keyed by a hash of the canonical query parameters (sub-ms)
  L2  semantic   embedding similarity >= threshold AND a structural guard (same ports / mode /
                 disruption fingerprint, weight & volume within tolerance). The guard stops a
                 "similar sounding" query for a different lane from reusing a wrong route.
  L3  persistent SQLite; survives restarts and rehydrates L2.

Lookup order is L1 -> L3 (exact primary-key read) -> L2 (approximate), so an exact answer always wins.

Entries record the ports/chokepoints their route touches, so a new disruption at any of those
locations evicts them (see invalidate_locations). Entries created while their route touched an
active disruption get a short TTL."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
from cachetools import TTLCache
from sqlalchemy import delete, select

from oceanbridge.cache.embeddings import Embedder, make_embedder
from oceanbridge.config import get_settings
from oceanbridge.core.db import CacheRow, session_scope
from oceanbridge.core.repository import utcnow


@dataclass
class CacheQuery:
    params: dict[str, Any]           # canonical parameters -> exact key
    text: str                        # natural-language rendering -> embedding
    guard: dict[str, Any]            # structural equivalence constraints
    touchpoints: list[str] = field(default_factory=list)

    def key(self, namespace: str) -> str:
        blob = json.dumps(self.params, sort_keys=True, default=str)
        return f"{namespace}:{hashlib.sha256(blob.encode()).hexdigest()[:32]}"


@dataclass
class CacheHit:
    payload: dict
    tier: str
    similarity: float = 1.0
    key: str = ""


NUMERIC_GUARD_FIELDS = ("weight_kg", "volume_cbm")


def guard_matches(a: dict, b: dict, tolerance: float) -> bool:
    if set(a) != set(b):
        return False
    for k, va in a.items():
        vb = b[k]
        if k in NUMERIC_GUARD_FIELDS:
            if max(abs(va), abs(vb)) == 0:
                continue
            if abs(va - vb) / max(abs(va), abs(vb)) > tolerance:
                return False
        elif va != vb:
            return False
    return True


class SemanticCache:
    def __init__(self, namespace: str, embedder: Embedder | None = None):
        cfg = get_settings().cache
        self.cfg = cfg
        self.namespace = namespace
        self.embedder = embedder or make_embedder()
        self._lock = threading.RLock()
        self._l1: TTLCache = TTLCache(maxsize=cfg.l1_max_entries, ttl=cfg.l1_ttl_seconds)
        self._l2_keys: list[str] = []
        self._l2_vecs: np.ndarray | None = None
        self._l2_meta: dict[str, dict] = {}
        self._hydrated = False
        self.stats = {"l1_hits": 0, "l2_hits": 0, "l3_hits": 0, "misses": 0, "puts": 0, "invalidated": 0,
                      "tokens_saved": 0}

    # ------------------------------------------------------------------ helpers
    def _hydrate(self) -> None:
        if self._hydrated:
            return
        now = utcnow()
        with session_scope() as s:
            rows = list(s.scalars(select(CacheRow).where(CacheRow.namespace == self.namespace,
                                                         CacheRow.expires_at > now)))
        for r in rows:
            if r.embedding is not None:
                vec = np.frombuffer(r.embedding, dtype=np.float32)
                self._l2_add(r.key, vec, {"guard": r.guard, "payload": r.payload, "expires_at": r.expires_at,
                                          "touchpoints": r.touchpoints})
        self._hydrated = True

    def _l2_add(self, key: str, vec: np.ndarray, meta: dict) -> None:
        if key in self._l2_meta:
            self._l2_remove({key})
        if self._l2_vecs is not None and self._l2_vecs.shape[1] != vec.shape[0]:
            return  # embedder changed; skip incompatible vectors
        self._l2_keys.append(key)
        self._l2_vecs = vec[None, :] if self._l2_vecs is None else np.vstack([self._l2_vecs, vec])
        self._l2_meta[key] = meta
        if len(self._l2_keys) > self.cfg.l2_max_entries:
            self._l2_remove({self._l2_keys[0]})

    def _l2_remove(self, keys: set[str]) -> None:
        if not keys or self._l2_vecs is None:
            return
        keep = [i for i, k in enumerate(self._l2_keys) if k not in keys]
        self._l2_keys = [self._l2_keys[i] for i in keep]
        self._l2_vecs = self._l2_vecs[keep] if keep else None
        for k in keys:
            self._l2_meta.pop(k, None)

    # ------------------------------------------------------------------ API
    def get(self, q: CacheQuery) -> CacheHit | None:
        key = q.key(self.namespace)
        with self._lock:
            self._hydrate()
            # L1 exact
            if key in self._l1:
                self.stats["l1_hits"] += 1
                return self._hit(CacheHit(self._l1[key], "L1-exact", 1.0, key))
            # L3 persistent exact: a primary-key lookup, checked before the approximate semantic tier because an
            # exact answer (e.g. after a restart or L1 eviction) always beats a similar one.
            with session_scope() as s:
                row = s.get(CacheRow, key)
                if row is not None and row.expires_at > utcnow():
                    row.hits += 1
                    self.stats["l3_hits"] += 1
                    self._l1[key] = row.payload
                    return self._hit(CacheHit(row.payload, "L3-persistent", 1.0, key))
            # L2 semantic (+ structural guard)
            if self._l2_vecs is not None and len(self._l2_keys):
                vec = self.embedder.embed(q.text)
                if vec.shape[0] == self._l2_vecs.shape[1]:
                    sims = self._l2_vecs @ vec
                    now = utcnow()
                    for i in np.argsort(-sims)[:10]:
                        sim = float(sims[i])
                        if sim < self.cfg.l2_similarity_threshold:
                            break
                        k = self._l2_keys[i]
                        meta = self._l2_meta[k]
                        if meta["expires_at"] <= now:
                            continue
                        if guard_matches(meta["guard"], q.guard, self.cfg.weight_tolerance):
                            self.stats["l2_hits"] += 1
                            self._l1[key] = meta["payload"]
                            return self._hit(CacheHit(meta["payload"], "L2-semantic", round(sim, 4), k))
            self.stats["misses"] += 1
            return None

    def _hit(self, hit: CacheHit) -> CacheHit:
        self.stats["tokens_saved"] += int(hit.payload.get("_tokens", 0) or 0)
        return hit

    def put(self, q: CacheQuery, payload: dict, disrupted: bool = False) -> str:
        key = q.key(self.namespace)
        ttl = self.cfg.disrupted_ttl_seconds if disrupted else self.cfg.l3_ttl_seconds
        now = utcnow()
        expires = now + timedelta(seconds=ttl)
        vec = self.embedder.embed(q.text)
        with self._lock:
            self._hydrate()
            self._l1[key] = payload
            self._l2_add(key, vec, {"guard": q.guard, "payload": payload, "expires_at": expires,
                                    "touchpoints": q.touchpoints})
            with session_scope() as s:
                row = s.get(CacheRow, key) or CacheRow(key=key, hits=0)
                row.namespace, row.text, row.embedding = self.namespace, q.text, vec.astype(np.float32).tobytes()
                row.payload, row.guard, row.touchpoints = payload, q.guard, q.touchpoints
                row.created_at, row.expires_at = now, expires
                s.merge(row)
            self.stats["puts"] += 1
        return key

    def invalidate_locations(self, locations: set[str]) -> int:
        """Evict every entry whose route touches one of the given ports/chokepoints."""
        if not locations:
            return 0
        with self._lock:
            self._hydrate()
            doomed = {k for k, m in self._l2_meta.items() if set(m["touchpoints"]) & locations}
            with session_scope() as s:
                for r in s.scalars(select(CacheRow).where(CacheRow.namespace == self.namespace)):
                    if set(r.touchpoints) & locations:
                        doomed.add(r.key)
                if doomed:
                    s.execute(delete(CacheRow).where(CacheRow.key.in_(doomed)))
            self._l2_remove(doomed)
            for k in doomed:
                self._l1.pop(k, None)
            self.stats["invalidated"] += len(doomed)
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._l1.clear()
            self._l2_keys, self._l2_vecs, self._l2_meta = [], None, {}
            with session_scope() as s:
                s.execute(delete(CacheRow).where(CacheRow.namespace == self.namespace))
            for k in self.stats:
                self.stats[k] = 0

    def snapshot(self) -> dict:
        with self._lock:
            lookups = self.stats["l1_hits"] + self.stats["l2_hits"] + self.stats["l3_hits"] + self.stats["misses"]
            hits = lookups - self.stats["misses"]
            return {**self.stats, "namespace": self.namespace, "embedder": self.embedder.name,
                    "l1_size": len(self._l1), "l2_size": len(self._l2_keys), "lookups": lookups,
                    "hit_rate": round(hits / lookups, 3) if lookups else 0.0}


_caches: dict[str, SemanticCache] = {}
_registry_lock = threading.Lock()


def get_cache(namespace: str) -> SemanticCache:
    with _registry_lock:
        if namespace not in _caches:
            _caches[namespace] = SemanticCache(namespace)
        return _caches[namespace]


def all_caches() -> dict[str, SemanticCache]:
    return dict(_caches)


def reset_caches() -> None:
    with _registry_lock:
        _caches.clear()

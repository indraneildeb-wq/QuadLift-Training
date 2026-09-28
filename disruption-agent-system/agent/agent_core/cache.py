import hashlib
import json
from difflib import SequenceMatcher

import redis


class SemanticCache:
    """Two-tier cache in front of LLM extraction calls, backed by Redis (a
    separate DB index from the state store so a cache flush can't touch
    in-flight report/timer state):

      - exact_match: sha256(normalized text) -> cached extraction, short TTL
      - semantic:    near-duplicate text (e.g. the same wire story syndicated
                     across outlets) reuses a prior extraction

    The semantic tier here compares normalized text directly (difflib ratio)
    rather than real embeddings, since it stands in for --mock-llm mode where
    there is no embedding model to call. Swap this for an embedding-similarity
    index once RealLLMClient is wired in for production."""

    def __init__(self, host, port, db, key_prefix, exact_ttl_seconds, semantic_ttl_seconds,
                 similarity_threshold, enabled=True):
        self._redis = redis.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
        self._prefix = key_prefix
        self.exact_ttl = exact_ttl_seconds
        self.semantic_ttl = semantic_ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.enabled = enabled

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())

    def _exact_key(self, text: str) -> str:
        digest = hashlib.sha256(self._normalize(text).encode("utf-8")).hexdigest()
        return f"{self._prefix}exact:{digest}"

    def get(self, text: str):
        """Returns (cached_value_or_None, hit: bool)."""
        if not self.enabled:
            return None, False

        exact_key = self._exact_key(text)
        hit = self._redis.get(exact_key)
        if hit:
            return json.loads(hit), True

        normalized = self._normalize(text)
        index_key = f"{self._prefix}semantic_index"
        for candidate in self._redis.hkeys(index_key):
            ratio = SequenceMatcher(None, normalized, candidate).ratio()
            if ratio >= self.similarity_threshold:
                cached_key = self._redis.hget(index_key, candidate)
                cached = self._redis.get(cached_key) if cached_key else None
                if cached:
                    return json.loads(cached), True
        return None, False

    def set(self, text: str, value: dict) -> None:
        if not self.enabled:
            return
        exact_key = self._exact_key(text)
        self._redis.set(exact_key, json.dumps(value), ex=self.exact_ttl)

        normalized = self._normalize(text)
        index_key = f"{self._prefix}semantic_index"
        self._redis.hset(index_key, normalized, exact_key)
        self._redis.expire(index_key, self.semantic_ttl)

"""Embedding backends for the semantic cache."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

from oceanbridge.config import get_settings


class Embedder(Protocol):
    name: str

    def embed(self, text: str) -> np.ndarray: ...


class LocalHashEmbedder:
    """Offline embedder: hashed word unigrams/bigrams + character trigrams, L2-normalised.

    Good enough to match paraphrased, near-identical route queries without any network call."""

    name = "local-hash-512"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _idx(self, token: str) -> tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        return h % self.dim, 1.0 if (h >> 63) & 1 else -1.0

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        words = re.findall(r"[a-z0-9]+", text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        joined = " ".join(words)
        feats += [joined[i:i + 3] for i in range(max(0, len(joined) - 2))]
        for f in feats:
            i, sign = self._idx(f)
            v[i] += sign
        n = np.linalg.norm(v)
        return v / n if n else v


class OpenAIEmbedder:
    def __init__(self, model: str):
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model
        self.name = model

    def embed(self, text: str) -> np.ndarray:
        resp = self.client.embeddings.create(model=self.model, input=text)
        v = np.asarray(resp.data[0].embedding, dtype=np.float32)
        return v / (np.linalg.norm(v) or 1.0)


def make_embedder() -> Embedder:
    s = get_settings()
    choice = s.cache.embedder
    if choice == "openai" or (choice == "auto" and s.use_crewai):
        try:
            return OpenAIEmbedder(s.models.embedding)
        except Exception:  # pragma: no cover - missing key / network
            pass
    return LocalHashEmbedder()

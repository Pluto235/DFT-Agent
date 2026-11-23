"""Embedder — sentence-transformers MiniLM-L6-v2 with BoW fallback.

Tries to load the vendored ST model from `data/models/all-MiniLM-L6-v2/`.
If absent or import fails, falls back to a zero-dependency hashed-shingle
BoW cosine model. The Experience layer is meant to be robust to ST being
unavailable in some envs (e.g. CI with limited disk).

Factory: `get_default_embedder(repo_root) -> Embedder`
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Iterable

import numpy as np


# ─────────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────────

class Embedder:
    """Abstract base — implementations must be deterministic for a given input."""

    dim: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return ndarray of shape (len(texts), self.dim), L2-normalised rows."""
        raise NotImplementedError

    @property
    def kind(self) -> str:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────
# Sentence-Transformers
# ─────────────────────────────────────────────────────────────────────

class STEmbedder(Embedder):
    """sentence-transformers loaded from a local model directory."""

    def __init__(self, model_dir: Path):
        from sentence_transformers import SentenceTransformer  # local import

        self._model = SentenceTransformer(str(model_dir), device="cpu")
        sample = self._model.encode(["dim probe"])
        self.dim = int(sample.shape[1])
        self._model_dir = Path(model_dir)

    @property
    def kind(self) -> str:
        return f"st:{self._model_dir.name}"

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = self._model.encode(list(texts), normalize_embeddings=True,
                                  show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────
# BoW fallback (zero deps)
# ─────────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class BoWEmbedder(Embedder):
    """Hashed-shingle bag-of-words. Deterministic, no external deps.

    Each token (lowercased) is hashed into `dim` buckets; bucket count is
    the feature value. L2-normalised at the end. Cosine similarity over
    these vectors is equivalent to a hashed Jaccard-ish similarity over
    token sets — crude but works on highly-formulaic material+calc keys.
    """

    def __init__(self, dim: int = 256):
        self.dim = int(dim)

    @property
    def kind(self) -> str:
        return f"bow:dim={self.dim}"

    def _hash_bucket(self, tok: str) -> int:
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=4).digest()
        return int.from_bytes(h, "little") % self.dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in _TOKEN_RE.findall(text.lower()):
                out[row, self._hash_bucket(tok)] += 1.0
        # L2-normalise
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return out / norms


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────

def get_default_embedder(repo_root: Path,
                          *, force_bow: bool = False) -> Embedder:
    """Try ST first; fall back to BoW. Never raises."""
    if not force_bow:
        model_dir = Path(repo_root) / "data" / "models" / "all-MiniLM-L6-v2"
        if model_dir.exists() and (model_dir / "config.json").exists():
            try:
                return STEmbedder(model_dir)
            except Exception:
                pass  # fall through to BoW
    return BoWEmbedder(dim=256)


def cosine_topk(query: np.ndarray, corpus: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (top-k indices, scores) given a single query row and a corpus.

    Both must be L2-normalised. Returns up to k results — fewer if corpus is smaller.
    """
    if corpus.size == 0:
        return np.array([], dtype=int), np.array([], dtype=np.float32)
    sims = corpus @ query.reshape(-1)
    k = min(k, corpus.shape[0])
    idx = np.argpartition(-sims, k - 1)[:k]
    # sort top-k by score desc
    idx = idx[np.argsort(-sims[idx])]
    return idx, sims[idx]

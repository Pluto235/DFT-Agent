"""v5 experience layer — cross-task memory (Voyager skill library pattern).

store.py     — JSONL append-only store + numpy embedding index
embed.py     — sentence-transformers offline embedding (+ BoW fallback)
recorder.py  — write one row per run (success + failure)
retriever.py — top-K retrieval by (material, calc_type, pseudo_family) embedding
"""

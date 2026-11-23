"""ExperienceStore — append-only JSONL + parallel numpy embedding index.

Schema (one row per past run):

    {
      "record_id": uuid,
      "task_id": "M1_T1_Si_vc_relax",
      "material_formula": "Si",
      "space_group_number": 227,
      "structure_family": "diamond" | "rocksalt" | ... | null,
      "calc_type": "vc_relax" | "bands_dos",
      "pseudo_family": "GBRV_USPP_PBE_v1.5",
      "params": {"ecutwfc": 60, "ecutrho": 480, "kpts": [8,8,8],
                 "mixing_beta": 0.7, "smearing": "mv", "degauss": 0.02},
      "success": true,
      "score": 1.0,                    # null until backfilled
      "walltime_s": 215.7,
      "fail_reason": null,
      "solver_version": "v5",
      "timestamp": "2026-05-13T12:00:00Z"
    }

Files (under <repo_root>/data/):
  - experience.jsonl       one record per line, append-only
  - experience.idx.npy     parallel ndarray (n_rows, embedding_dim)
  - experience.keys.txt    parallel list of embedding-key strings (debugging)

Atomic update: on append, the .npy is rewritten with the new row appended,
then renamed over the old file. JSONL is appended in place (single-writer
assumption — only solve.py writes after a run).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

from agent.experience.embed import Embedder, cosine_topk


# Fields that are GRADED bench outputs, not methodology. They must never be
# persisted or surfaced into a prompt — replaying them for the same material
# is answer leakage (e.g. space_group_number is 0.25 of the T1 score).
ANSWER_FIELDS: tuple[str, ...] = ("space_group_number",)


def _compute_cost_proxy(params: dict | None) -> float | None:
    """Operation-count proxy C ≈ ecutwfc**1.5 * (kx*ky*kz).

    Diagnostic only — logged, never surfaced into a prompt or used as an
    optimization target (a cheaper-but-wrong config loses the bench). Returns
    None unless ecutwfc and a 3-element kpts grid are both present (band/kpath
    runs carry kpts=None).
    """
    if not params:
        return None
    ec = params.get("ecutwfc")
    k = params.get("kpts")
    if ec is None or not isinstance(k, (list, tuple)) or len(k) != 3:
        return None
    try:
        return float(ec) ** 1.5 * (float(k[0]) * float(k[1]) * float(k[2]))
    except (TypeError, ValueError):
        return None


@dataclass
class ExperienceRecord:
    task_id: str
    material_formula: str
    calc_type: str
    pseudo_family: str
    params: dict
    success: bool
    walltime_s: float = 0.0
    cost_proxy: float | None = None   # ecutwfc**1.5 * prod(kpts); diagnostic only
    score: float | None = None
    space_group_number: int | None = None
    structure_family: str | None = None
    fail_reason: str | None = None
    solver_version: str = "v5"
    timestamp: str = ""
    record_id: str = ""

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Compute only when absent so a value loaded from an old row (or set
        # explicitly) is preserved; recomputes for legacy rows that stored
        # params but no cost_proxy (free backfill on reload).
        if self.cost_proxy is None:
            self.cost_proxy = _compute_cost_proxy(self.params)

    def scrub_answers(self) -> "ExperienceRecord":
        """Null out any graded-output field (anti-leak). Returns self."""
        for f in ANSWER_FIELDS:
            setattr(self, f, None)
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def embedding_key(self) -> str:
        """Stable text key used for embedding similarity matching.

        Deliberately excludes graded outputs (space_group_number) — similarity
        keys off material/calc/pseudo/family only, never off an answer.
        """
        parts: list[str] = [self.material_formula, self.calc_type, self.pseudo_family]
        if self.structure_family:
            parts.append(self.structure_family)
        return " ".join(parts)


class ExperienceStore:
    """Persistent store. Lazy-loaded from disk on first query."""

    JSONL_NAME = "experience.jsonl"
    NPY_NAME = "experience.idx.npy"
    KEYS_NAME = "experience.keys.txt"

    def __init__(self, data_dir: Path, embedder: Embedder):
        self.data_dir = Path(data_dir)
        self.embedder = embedder
        self.records: list[ExperienceRecord] = []
        self._matrix: np.ndarray | None = None
        self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        jp = self.data_dir / self.JSONL_NAME
        if not jp.exists():
            return
        for line in jp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self.records.append(ExperienceRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # skip corrupt rows

        np_path = self.data_dir / self.NPY_NAME
        if np_path.exists():
            try:
                self._matrix = np.load(np_path)
                if self._matrix.shape[0] != len(self.records):
                    self._matrix = None  # stale; rebuild on demand
            except Exception:
                self._matrix = None

    def _rebuild_matrix(self) -> None:
        if not self.records:
            self._matrix = np.zeros((0, self.embedder.dim), dtype=np.float32)
            return
        keys = [r.embedding_key() for r in self.records]
        self._matrix = self.embedder.encode(keys)

    def _persist(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # JSONL: write all rows (small, atomic via tmp+rename)
        tmp = self.data_dir / (self.JSONL_NAME + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(r.to_dict(), default=str) + "\n")
        tmp.replace(self.data_dir / self.JSONL_NAME)

        # NPY: tmp+rename. np.save auto-appends ".npy" unless given a file
        # handle, so we open the tmp path manually to keep our chosen name.
        if self._matrix is not None:
            tmp_npy = self.data_dir / (self.NPY_NAME + ".tmp")
            with tmp_npy.open("wb") as fh:
                np.save(fh, self._matrix)
            tmp_npy.replace(self.data_dir / self.NPY_NAME)

        # Debug-friendly keys file
        keys_tmp = self.data_dir / (self.KEYS_NAME + ".tmp")
        keys_tmp.write_text("\n".join(r.embedding_key() for r in self.records),
                            encoding="utf-8")
        keys_tmp.replace(self.data_dir / self.KEYS_NAME)

    # ---- public API --------------------------------------------------------

    def add(self, rec: ExperienceRecord) -> None:
        rec.scrub_answers()   # anti-leak: never persist graded outputs
        self.records.append(rec)
        vec = self.embedder.encode([rec.embedding_key()])
        if self._matrix is None or self._matrix.shape[0] == 0:
            self._matrix = vec
        else:
            # Embedding dim mismatch (e.g. switched ST↔BoW) → drop old matrix
            if self._matrix.shape[1] != vec.shape[1]:
                self._rebuild_matrix()
            else:
                self._matrix = np.vstack([self._matrix, vec])
        self._persist()

    def backfill_score(self, task_id: str, score: float) -> bool:
        """Update the most-recent record for `task_id` with a bench score.
        Returns True if a row was patched."""
        for r in reversed(self.records):
            if r.task_id == task_id and r.score is None:
                r.score = float(score)
                self._persist()
                return True
        return False

    def query(
        self,
        *,
        material_formula: str,
        calc_type: str,
        pseudo_family: str | None = None,
        space_group_number: int | None = None,
        structure_family: str | None = None,
        k: int = 5,
        min_score: float | None = 0.7,
        only_success: bool = True,
        exclude_task_id: str | None = None,
    ) -> list[ExperienceRecord]:
        """Top-K similar records, post-filtered by success/score.

        `exclude_task_id` drops rows from that exact task (cross-validation
        hygiene — never retrieve the bench's own prior answer for this task).
        """
        if not self.records:
            return []
        if self._matrix is None:
            self._rebuild_matrix()
        assert self._matrix is not None

        query_key = " ".join([material_formula, calc_type, pseudo_family or "",
                              f"sg{space_group_number}" if space_group_number else "",
                              structure_family or ""])
        qv = self.embedder.encode([query_key])[0]
        idx, sims = cosine_topk(qv, self._matrix, max(k * 4, k))  # over-fetch for filtering

        out: list[ExperienceRecord] = []
        for i in idx:
            r = self.records[int(i)]
            if exclude_task_id is not None and r.task_id == exclude_task_id:
                continue
            if only_success and not r.success:
                continue
            if min_score is not None and r.score is not None and r.score < min_score:
                continue
            out.append(r)
            if len(out) >= k:
                break
        return out

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[ExperienceRecord]:
        return iter(self.records)

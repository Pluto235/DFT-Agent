"""Recorder — solve.py finally-block helpers.

Called once per solver subprocess (regardless of success/failure) to
append a row to the experience store. Score is left null until
`scripts/backfill_scores.py` runs after a bench batch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.experience.embed import Embedder, get_default_embedder
from agent.experience.store import ExperienceRecord, ExperienceStore


_TASK_ID_FORMULA = re.compile(r"^M\d_T\d_([^_]+)_", re.IGNORECASE)
_PSEUDO_FAMILY = "GBRV_USPP_PBE_v1.5"  # bench-mandated default; override if needed


def extract_material_formula(task_id: str) -> str:
    """M1_T1_Si_vc_relax → 'Si'; M2_T2_CsPbBr3_bands_dos → 'CsPbBr3'."""
    m = _TASK_ID_FORMULA.match(task_id)
    return m.group(1) if m else task_id


def infer_calc_type_from_outputs(required_keys: list[str]) -> str:
    if "vc_relax_out" in required_keys:
        return "vc_relax"
    if {"scf_out", "bands_out", "bands_data", "dos_dat"}.issubset(set(required_keys)):
        return "bands_dos"
    return "unknown"


def record_run(
    *,
    repo_root: Path,
    task_id: str,
    required_keys: list[str],
    success: bool,
    walltime_s: float,
    params: dict | None = None,
    fail_reason: str | None = None,
    space_group_number: int | None = None,
    structure_family: str | None = None,
    pseudo_family: str = _PSEUDO_FAMILY,
    embedder: Embedder | None = None,
) -> ExperienceRecord:
    """Append a row. Idempotent on file-not-exists; safe to call in finally."""
    data_dir = Path(repo_root) / "data"
    emb = embedder or get_default_embedder(Path(repo_root))
    store = ExperienceStore(data_dir, emb)
    rec = ExperienceRecord(
        task_id=task_id,
        material_formula=extract_material_formula(task_id),
        calc_type=infer_calc_type_from_outputs(required_keys),
        pseudo_family=pseudo_family,
        params=params or {},
        success=success,
        walltime_s=float(walltime_s),
        fail_reason=fail_reason,
        space_group_number=space_group_number,
        structure_family=structure_family,
    )
    store.add(rec)
    return rec

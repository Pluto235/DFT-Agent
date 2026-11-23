"""Retriever — format top-K experience records into a prompt block.

Called by solve.py BEFORE building SDK options. The returned text is
prepended to the user prompt so the strategist + executor see relevant
past successes inline. Empty block (returned as `""`) if no matches.
"""

from __future__ import annotations

from pathlib import Path

from agent.experience.embed import get_default_embedder
from agent.experience.recorder import extract_material_formula, infer_calc_type_from_outputs
from agent.experience.store import ExperienceRecord, ExperienceStore


def format_experience_block(records: list[ExperienceRecord]) -> str:
    if not records:
        return ""
    lines: list[str] = ["=== Past similar runs (filtered to success ≥ 0.7 score) ==="]
    for r in records:
        score = f"{r.score:.2f}" if r.score is not None else "n/a"
        p = r.params or {}
        # k-mesh field name varies: write_qe_input emits the 3-tuple `kpts`,
        # but the seed script only recovers `n_kpts` (count) from .out headers.
        k_str = p.get("kpts") or (f"~{p['n_kpts']} pts" if p.get("n_kpts") else "?")
        knobs = (
            f"ecut={p.get('ecutwfc')}/{p.get('ecutrho')} Ry, "
            f"kpts={k_str}, "
            f"occ={p.get('occupations') or '?'}"
        )
        if p.get("smearing"):
            knobs += f", smearing={p['smearing']}@{p.get('degauss')}"
        # NB: never echo graded outputs (e.g. space_group_number) here — only
        # methodology. Surfacing the answer for the same material is leakage.
        lines.append(
            f"- {r.material_formula} ({r.calc_type}): "
            f"score={score}, {knobs}, walltime={r.walltime_s:.0f}s"
        )
    lines.append("Use these parameter choices as starting point unless you have "
                 "strong reason to deviate.")
    return "\n".join(lines)


def retrieve_for_task(
    *,
    repo_root: Path,
    task_id: str,
    required_keys: list[str],
    k: int = 3,
) -> tuple[list[ExperienceRecord], str]:
    """Returns (records, formatted_block).

    The block is meant to be prepended to user_prompt. Empty string when
    no matches survive filtering.
    """
    data_dir = Path(repo_root) / "data"
    emb = get_default_embedder(Path(repo_root))
    store = ExperienceStore(data_dir, emb)
    if not store.records:
        return [], ""

    material = extract_material_formula(task_id)
    calc_type = infer_calc_type_from_outputs(required_keys)
    candidates = store.query(
        material_formula=material,
        calc_type=calc_type,
        k=k,
        only_success=True,
        min_score=0.7,
        exclude_task_id=task_id,   # never retrieve this task's own prior answer
    )
    return candidates, format_experience_block(candidates)

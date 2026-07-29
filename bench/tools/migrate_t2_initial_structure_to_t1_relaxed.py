"""Migration: rewrite T2 question JSON's `material.initial_structure` lattice
from MP raw → T1 canonical GT relaxed values.

Background (2026-05-19, 杨博 decision):
  - Current T2 prompt 把 MP raw lattice 喂给 agent (e.g. Si a=3.849 Å)
  - 但 T2 GT pipeline (canonical_gt_t2) 用的是 T1 vc-relax 弛豫后的 lattice
    (Si a=3.866 Å, per t2_params.py:4 docstring "Geometry is taken from
    the T1 canonical GT (final relaxed lattice parameters)")
  - 这个错位让 agent 不主动 vc-relax 时电子结构必然与 GT 偏（systematic bias）
  - Fix: T2 prompt 给的 initial_structure 也用 T1 relaxed lattice，保持
    prompt/GT 结构一致。Atomic fractional positions 不变（T1 vc-relax 不动
    fractional positions, per t2_params.py:123）。

What this migration does:
  - For each `is_stable=true` T2 question in M1_T2/:
    1. Find corresponding M*_T1_<mat>_vc_relax canonical_gt
    2. If T1 GT has ground_truth.{a,b,c,alpha,beta,gamma}.value → use those
    3. Write back to T2 JSON `material.initial_structure.lattice_constants_angstrom`
       + `lattice_angles_degree`
  - Skip is_stable=false (handled by m12-t2-rescue-7-failed-gt rescue + others)
  - Skip if T1 GT missing/failed (no relaxed lattice to migrate)

What this migration does NOT do:
  - 不动 canonical_gt_t2/ 或 dos_data/（T2 GT 数据保持原样）
  - 不动 atomic_positions_crystal（T1 vc-relax 不改 fractional positions）
  - 不动 source / note / lattice_type 字段（保持 v1.2 "不暴露 pre-relaxed" 语义）
  - 不动 ground_truth / scoring（这次只改 prompt 输入结构）

Idempotent: re-running migration overwrites with same values → no diff.
"""
from __future__ import annotations

import json
from pathlib import Path


BENCH_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_DIR = BENCH_ROOT / "questions"
T1_GT_DIR = BENCH_ROOT / "tools" / "gt_selfrun" / "canonical_gt"


def _load_t1_relaxed_lattice(t1_qid: str) -> dict[str, float] | None:
    """Return {a,b,c,alpha,beta,gamma} from M*_T1_<mat>_vc_relax canonical_gt,
    or None if missing / failed (no ground_truth block / no 'a' key)."""
    p = T1_GT_DIR / f"{t1_qid}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    gt = d.get("ground_truth") or {}
    if "a" not in gt:
        return None
    out = {}
    for k in ("a", "b", "c", "alpha", "beta", "gamma"):
        cell = gt.get(k)
        if cell is None or "value" not in cell:
            return None
        out[k] = float(cell["value"])
    return out


def _t1_qid_for_t2(t2_qid: str) -> str:
    """M1_T2_Si_bands_dos → M1_T1_Si_vc_relax"""
    return t2_qid.replace("_T2_", "_T1_").replace("_bands_dos", "_vc_relax")


def migrate_one(t2_path: Path) -> tuple[str, dict | None, str | None]:
    """Returns (status, diff_summary, error).
    status ∈ {'updated','skipped_unstable','skipped_no_t1','no_change'}"""
    q = json.loads(t2_path.read_text(encoding="utf-8"))
    is_stable = q.get("metadata", {}).get("is_stable", True)
    if not is_stable:
        return "skipped_unstable", None, None
    t1_qid = _t1_qid_for_t2(t2_path.stem)
    lat = _load_t1_relaxed_lattice(t1_qid)
    if lat is None:
        return "skipped_no_t1", None, f"T1 GT missing/failed: {t1_qid}"

    init = q["material"]["initial_structure"]
    old_lc = init.get("lattice_constants_angstrom", {})
    old_la = init.get("lattice_angles_degree", {})

    new_lc = {"a": lat["a"], "b": lat["b"], "c": lat["c"]}
    new_la = {"alpha": lat["alpha"], "beta": lat["beta"], "gamma": lat["gamma"]}

    if old_lc == new_lc and old_la == new_la:
        return "no_change", None, None

    init["lattice_constants_angstrom"] = new_lc
    init["lattice_angles_degree"] = new_la

    t2_path.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    diff = {
        "lc_before": old_lc,
        "lc_after": new_lc,
        "la_before": old_la,
        "la_after": new_la,
    }
    return "updated", diff, None


def main() -> int:
    paths = sorted((QUESTIONS_DIR / "M1_T2").glob("*.json"))
    n_total = len(paths)
    n_updated = n_no_change = n_unstable = n_no_t1 = 0
    largest_delta = (0.0, "", {})

    for p in paths:
        status, diff, err = migrate_one(p)
        tid = p.stem
        if status == "updated":
            n_updated += 1
            # compute max |Δa/a_old| for audit
            la = diff["lc_after"]["a"]
            lo = diff["lc_before"]["a"]
            if lo > 0:
                rel = abs(la - lo) / lo
                if rel > largest_delta[0]:
                    largest_delta = (rel, tid, diff)
            print(f"  UPDATED  {tid}  "
                  f"a {diff['lc_before']['a']:.3f}→{diff['lc_after']['a']:.3f}")
        elif status == "no_change":
            n_no_change += 1
        elif status == "skipped_unstable":
            n_unstable += 1
        elif status == "skipped_no_t1":
            n_no_t1 += 1
            print(f"  SKIP-NO-T1  {tid}: {err}")

    print()
    print(f"[migrate] summary: total={n_total} "
          f"updated={n_updated} no_change={n_no_change} "
          f"skipped_unstable={n_unstable} skipped_no_t1={n_no_t1}")
    if largest_delta[1]:
        rel, tid, diff = largest_delta
        print(f"[migrate] largest Δa: {rel*100:.2f}% on {tid}  "
              f"(a {diff['lc_before']['a']:.3f}→{diff['lc_after']['a']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

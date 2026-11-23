"""Curve compare data utility for result page viz.

Loads pred (from report.json extracted_fields) + gt (from ref_file path in
question JSON ground_truth) for dos_curve-style fields (T2 dos_curve etc.);
computes F1 / Dice area-overlap score + precision / recall + area diagnostics
+ peak summary metadata for UI.

Algorithm mirrors harness/lib/scoring.py:_score_curve_f1 (2026-05-17 decision)：
  1. sort pred + interpolate onto GT grid (np.interp, out-of-range → 0)
  2. clip both pred & GT to [0, +∞) (negative DOS = numeric noise)
  3. compute three areas via np.trapezoid (np.trapz fallback for numpy<2):
       A_pred  = ∫ pred dE        (over GT range)
       A_gt    = ∫ gt   dE
       A_int   = ∫ min(pred, gt) dE
  4. P = A_int / A_pred；R = A_int / A_gt；F1 = 2PR / (P+R)
  5. A_pred ≤ 0 or A_gt ≤ 0 → ValueError (degenerate; caller maps to 4xx)

No horizontal-shift scan, no normalization, no tolerance — score = F1 directly.

ref_file paths in question JSONs use 'bench/...' prefix (workspace-rooted),
files actually live under bench_dir; we resolve by stripping 'bench/' prefix
and trying bench_dir as base. Mirrors scoring._resolve_ref_file behavior.

Public function: compute_curve_compare().
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


def _resolve_ref_file(ref: str, bench_dir: Path) -> Optional[Path]:
    """Resolve GT ref_file path relative to bench_dir.

    Tries: ref absolute → ref under bench_dir → ref with 'bench/' prefix
    stripped under bench_dir.
    """
    p = Path(ref)
    if p.is_absolute():
        return p.resolve() if p.exists() else None
    candidates = [bench_dir / p]
    if str(p).startswith("bench/"):
        stripped = Path(str(p)[len("bench/"):])
        candidates.append(bench_dir / stripped)
    for c in candidates:
        if c.exists():
            return c.resolve()
    return None


@lru_cache(maxsize=128)
def _read_2col_dat(path_str: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Read 2-col ASCII (skip # comments). Cached by path string for repeat hits."""
    xs: list[float] = []
    ys: list[float] = []
    text = Path(path_str).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        try:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
        except ValueError:
            continue
    return tuple(xs), tuple(ys)


def compute_curve_compare(
    pred: Any,
    gt_value: Any,
    tolerance: float,
    bench_dir: Path,
    *,
    task_id: str | None = None,
    gt_reference_ev: float | None = None,
) -> dict[str, Any]:
    """Build curve compare payload for UI.

    Returns dict with pred/gt arrays + F1 score + precision/recall + area
    diagnostics + summary (peaks). Raises ValueError on shape / data issues
    (caller maps to HTTP 4xx/5xx). tolerance is accepted for signature
    compatibility but unused (F1 is the direct score).

    If task_id + gt_reference_ev given, additionally loads
    `tools/gt_selfrun/canonical_gt_t2/dos_data/<material>.dat` (full raw
    absolute energy GT) and returns it as `gt_full` (E - gt_reference_ev),
    for showing extended-range visualization with scoring window highlight.
    """
    import numpy as np  # heavy, local import

    if not isinstance(pred, dict):
        raise ValueError("pred must be a dict with energy_ev / dos")
    e_pred_l = pred.get("energy_ev")
    y_pred_l = pred.get("dos")
    if not isinstance(e_pred_l, list) or not isinstance(y_pred_l, list):
        raise ValueError("pred missing energy_ev / dos lists")
    if len(e_pred_l) != len(y_pred_l) or len(e_pred_l) < 2:
        raise ValueError(
            f"pred curve too short or mismatched (len={len(e_pred_l)}/{len(y_pred_l)})"
        )

    if not isinstance(gt_value, dict) or "ref_file" not in gt_value:
        raise ValueError("gt_value missing ref_file")
    ref_path = _resolve_ref_file(gt_value["ref_file"], bench_dir)
    if ref_path is None:
        raise ValueError(f"ref_file not found: {gt_value['ref_file']}")

    e_gt_t, y_gt_t = _read_2col_dat(str(ref_path))
    if len(e_gt_t) < 2:
        raise ValueError(f"ref_file too few points: {ref_path}")

    # numpy 2.0 起 np.trapz 被 np.trapezoid 取代；兼容两者。
    _trapezoid = getattr(np, "trapezoid", np.trapz)

    e_pred = np.asarray(e_pred_l, dtype=float)
    y_pred = np.asarray(y_pred_l, dtype=float)
    e_gt = np.asarray(e_gt_t, dtype=float)
    y_gt = np.asarray(y_gt_t, dtype=float)

    o_pred = np.argsort(e_pred)
    e_pred, y_pred = e_pred[o_pred], y_pred[o_pred]
    o_gt = np.argsort(e_gt)
    e_gt, y_gt = e_gt[o_gt], y_gt[o_gt]

    # 积分窗口 = GT 横轴范围；pred 越界 → 0（避免 np.interp 默认端点常值外推）
    y_pred_on_gt = np.interp(e_gt, e_pred, y_pred, left=0.0, right=0.0)
    # 负 DOS 视为数值噪声，clip 到 0
    y_pred_on_gt = np.clip(y_pred_on_gt, 0.0, None)
    y_gt_clipped = np.clip(y_gt, 0.0, None)

    A_pred = float(_trapezoid(y_pred_on_gt, e_gt))
    A_gt = float(_trapezoid(y_gt_clipped, e_gt))
    A_int = float(_trapezoid(np.minimum(y_pred_on_gt, y_gt_clipped), e_gt))

    if A_pred <= 0 or A_gt <= 0:
        raise ValueError(
            f"degenerate curve: A_pred={A_pred:.6g}, A_gt={A_gt:.6g} (need both > 0)"
        )

    P = A_int / A_pred
    R = A_int / A_gt
    F1 = 2 * P * R / (P + R) if (P + R) > 0 else 0.0

    pred_peak_i = int(np.argmax(y_pred))
    gt_peak_i = int(np.argmax(y_gt))
    pred_peak = {"e_ev": float(e_pred[pred_peak_i]), "dos": float(y_pred[pred_peak_i])}
    gt_peak = {"e_ev": float(e_gt[gt_peak_i]), "dos": float(y_gt[gt_peak_i])}
    peak_e_shift = pred_peak["e_ev"] - gt_peak["e_ev"]
    peak_dos_ratio = pred_peak["dos"] / gt_peak["dos"] if gt_peak["dos"] > 0 else 0.0

    # Optional: extended-range GT (full canonical raw, relative to gt_reference_ev)
    gt_full = None
    scoring_window = None
    if task_id and gt_reference_ev is not None:
        try:
            # task_id like M1_T2_LiCl_bands_dos → material name LiCl
            parts = task_id.replace("_bands_dos", "").split("_", 2)
            if len(parts) >= 3:
                mat = parts[2]
                full_dat = bench_dir / "tools/gt_selfrun/canonical_gt_t2/dos_data" / f"{mat}.dat"
                if full_dat.exists():
                    e_full_t, y_full_t = _read_2col_dat(str(full_dat))
                    e_full = np.asarray(e_full_t, dtype=float) - float(gt_reference_ev)
                    y_full = np.clip(np.asarray(y_full_t, dtype=float), 0.0, None)
                    o = np.argsort(e_full)
                    gt_full = {
                        "energy_ev": e_full[o].tolist(),
                        "dos": y_full[o].tolist(),
                    }
                    # The scoring window is the range of e_gt (trimmed ref_file)
                    scoring_window = {
                        "e_min": float(e_gt.min()),
                        "e_max": float(e_gt.max()),
                    }
        except Exception:
            pass  # extended visualization is optional; ignore errors

    return {
        "pred": {"energy_ev": e_pred.tolist(), "dos": y_pred.tolist()},
        "gt": {"energy_ev": e_gt.tolist(), "dos": y_gt.tolist()},
        "gt_full": gt_full,                   # extended raw GT, relative to VBM (or None)
        "scoring_window": scoring_window,     # {e_min, e_max} of the F1 integration range
        "score": round(F1, 6),
        "precision": round(P, 6),
        "recall": round(R, 6),
        "A_pred": round(A_pred, 6),
        "A_gt": round(A_gt, 6),
        "A_intersect": round(A_int, 6),
        "summary": {
            "n_pred": len(e_pred),
            "n_gt": len(e_gt),
            "pred_peak": pred_peak,
            "gt_peak": gt_peak,
            "peak_e_shift_ev": round(peak_e_shift, 4),
            "peak_dos_ratio": round(peak_dos_ratio, 4),
        },
    }

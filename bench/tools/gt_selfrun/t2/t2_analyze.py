"""T2 8-point scan convergence analysis.

For each material's 8 scan-point summaries (output of t2_run.run_one_scan_point),
compute the last-two-point Δ for both `band_gap_ev` and `dos_e_fermi`, in two
rounds (ecutwfc round 1, kmesh round 2), and classify the material into one
of: converged / marginal / not_converged.

Thresholds are tunable (杨博 will lock them after pilot review). The defaults
here are pre-pilot placeholders chosen to be informative but not
production-final:

  - band_gap: |Δgap| < 0.02 eV (< judge tolerance 0.1 eV / 5)
  - e_fermi:  |ΔEf|  < 0.02 eV (same scale)

Marginal = within 2× the converged threshold; not_converged = beyond.
"""
from __future__ import annotations

from typing import Any

# Defaults — will be replaced after A.3 pilot + 杨博 review.
DEFAULT_BAND_GAP_THR_EV = 0.02      # converged < this
DEFAULT_E_FERMI_THR_EV = 0.02       # converged < this
MARGINAL_FACTOR = 2.0                # marginal: between 1× and MARGINAL_FACTOR×


def _last_two(points_round: list[dict], key: str) -> tuple[float | None, float | None]:
    """Return (value_at_penultimate, value_at_last) for a given key.

    points_round must be ordered by ecutwfc (round 1) or kmesh_density (round 2).
    """
    if len(points_round) < 2:
        return (None, None)
    p_last = points_round[-1]
    p_prev = points_round[-2]
    v_last = p_last.get("extracted", {}).get(key)
    v_prev = p_prev.get("extracted", {}).get(key)
    return (v_prev, v_last)


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return abs(a - b)


def classify_convergence(
    point_summaries: list[dict],
    *,
    band_gap_thr_ev: float = DEFAULT_BAND_GAP_THR_EV,
    e_fermi_thr_ev: float = DEFAULT_E_FERMI_THR_EV,
    marginal_factor: float = MARGINAL_FACTOR,
) -> dict:
    """Classify a material's 8-point T2 scan as converged / marginal / not_converged.

    Args:
        point_summaries: list of 8 dicts (output of run_one_scan_point), each
            containing keys {point_key, round, ecutwfc_ry, kmesh_density,
            extracted: {band_gap_ev, dos_e_fermi}}.
        band_gap_thr_ev: converged threshold for |Δgap| (default 0.02 eV)
        e_fermi_thr_ev: converged threshold for |ΔEf| (default 0.02 eV)
        marginal_factor: marginal upper bound = thr × this (default 2.0)

    Returns:
        {
            "classification": "converged" | "marginal" | "not_converged",
            "round1_ecutwfc": {
                "delta_band_gap_ev": float | None,
                "delta_e_fermi_ev": float | None,
                "last_two_keys": [str, str],   # e.g. ["e60_d30", "e80_d30"]
            },
            "round2_kmesh": {
                "delta_band_gap_ev": float | None,
                "delta_e_fermi_ev": float | None,
                "last_two_keys": [str, str],   # e.g. ["e80_d40", "e80_d50"]
            },
            "thresholds": {"band_gap_thr_ev": ..., "e_fermi_thr_ev": ...,
                          "marginal_factor": ...},
            "reason": str,    # human-readable; what triggered the classification
        }
    """
    # Filter out failed points; sort by physics knob
    round1 = [p for p in point_summaries if p.get("round") == 1
              and p.get("status") == "OK"]
    round2 = [p for p in point_summaries if p.get("round") == 2
              and p.get("status") == "OK"]
    round1.sort(key=lambda p: p["ecutwfc_ry"])
    round2.sort(key=lambda p: p["kmesh_density"])

    r1_gap_prev, r1_gap_last = _last_two(round1, "band_gap_ev")
    r1_ef_prev, r1_ef_last = _last_two(round1, "dos_e_fermi")
    r2_gap_prev, r2_gap_last = _last_two(round2, "band_gap_ev")
    r2_ef_prev, r2_ef_last = _last_two(round2, "dos_e_fermi")

    d_r1_gap = _delta(r1_gap_prev, r1_gap_last)
    d_r1_ef = _delta(r1_ef_prev, r1_ef_last)
    d_r2_gap = _delta(r2_gap_prev, r2_gap_last)
    d_r2_ef = _delta(r2_ef_prev, r2_ef_last)

    # If any required delta is missing, classify as not_converged with reason
    deltas = [
        ("r1_gap", d_r1_gap, band_gap_thr_ev),
        ("r1_ef", d_r1_ef, e_fermi_thr_ev),
        ("r2_gap", d_r2_gap, band_gap_thr_ev),
        ("r2_ef", d_r2_ef, e_fermi_thr_ev),
    ]
    missing = [name for name, d, thr in deltas if d is None]
    if missing:
        return {
            "classification": "not_converged",
            "round1_ecutwfc": _round_block(round1, "ecutwfc_ry",
                                          d_r1_gap, d_r1_ef),
            "round2_kmesh": _round_block(round2, "kmesh_density",
                                        d_r2_gap, d_r2_ef),
            "thresholds": {
                "band_gap_thr_ev": band_gap_thr_ev,
                "e_fermi_thr_ev": e_fermi_thr_ev,
                "marginal_factor": marginal_factor,
            },
            "reason": f"missing deltas: {missing}",
        }

    # All deltas present
    converged_all = all([
        d_r1_gap < band_gap_thr_ev,
        d_r1_ef < e_fermi_thr_ev,
        d_r2_gap < band_gap_thr_ev,
        d_r2_ef < e_fermi_thr_ev,
    ])
    marginal_any = any([
        d_r1_gap >= band_gap_thr_ev * marginal_factor,
        d_r1_ef >= e_fermi_thr_ev * marginal_factor,
        d_r2_gap >= band_gap_thr_ev * marginal_factor,
        d_r2_ef >= e_fermi_thr_ev * marginal_factor,
    ])
    if converged_all:
        cls = "converged"
        reason = "all 4 deltas within converged thresholds"
    elif marginal_any:
        cls = "not_converged"
        # find which exceeded
        offenders = []
        for name, d, thr in [
            ("r1_gap", d_r1_gap, band_gap_thr_ev),
            ("r1_ef", d_r1_ef, e_fermi_thr_ev),
            ("r2_gap", d_r2_gap, band_gap_thr_ev),
            ("r2_ef", d_r2_ef, e_fermi_thr_ev),
        ]:
            if d is not None and d >= thr * marginal_factor:
                offenders.append(f"{name}={d:.4g} (≥{thr * marginal_factor:.4g})")
        reason = "exceeded marginal: " + "; ".join(offenders)
    else:
        cls = "marginal"
        reason = "between converged and marginal thresholds"

    return {
        "classification": cls,
        "round1_ecutwfc": _round_block(round1, "ecutwfc_ry",
                                      d_r1_gap, d_r1_ef),
        "round2_kmesh": _round_block(round2, "kmesh_density",
                                    d_r2_gap, d_r2_ef),
        "thresholds": {
            "band_gap_thr_ev": band_gap_thr_ev,
            "e_fermi_thr_ev": e_fermi_thr_ev,
            "marginal_factor": marginal_factor,
        },
        "reason": reason,
    }


def _round_block(
    round_points: list[dict],
    sort_key: str,
    d_gap: float | None,
    d_ef: float | None,
) -> dict:
    """Build the round-summary block with last-two keys."""
    keys = [p["point_key"] for p in round_points]
    last_two = keys[-2:] if len(keys) >= 2 else keys
    return {
        "delta_band_gap_ev": d_gap,
        "delta_e_fermi_ev": d_ef,
        "last_two_keys": last_two,
        "scan_keys": keys,
    }

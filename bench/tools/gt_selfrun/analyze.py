"""Aggregate QE scan outputs → canonical GT + convergence_evidence.

Split into:
- parse_relax_output: single-file parser for one vc-relax output
- find_convergence: determine overall convergence from 2 scan rounds
- (more functions in later tasks: build_canonical_gt, main)
"""
from __future__ import annotations

import re
from pathlib import Path

from pymatgen.core import Lattice, Structure
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer


BOHR_TO_ANG = 0.52917721092


# Convergence thresholds (strategy A, §4.4 of settings_plan.md)
DELTA_E_MEV_PER_ATOM_THR = 0.5    # meV/atom (was 0.05; relaxed 10× — see Phase 1 empirical distribution)
DELTA_P_KBAR_THR = 0.5             # kbar

# Plan B 收敛判据 — 几何相对差（杨博 0427 拍板：1% 相对已足够，与判分容差同档）。
# 相对差定义：max(|Δa|/|a|, |Δb|/|b|, |Δc|/|c|) over last two scan points.
DELTA_L_REL_CONVERGED = 0.01       # < 1% → converged
DELTA_L_REL_MARGINAL = 0.02        # 1–2% → marginal；≥ 2% → not_converged

# Deprecated absolute Å thresholds（保留常量便于旧报告/读者识别历史；分类逻辑已切到相对）
DELTA_A_CONVERGED_AA = 0.001       # Å, 旧版 converged 上限（已弃用）
DELTA_A_MARGINAL_AA = 0.005        # Å, 旧版 marginal 上限（已弃用）

RY_TO_MEV = 13605.693  # 1 Ry = 13605.693 meV

# Tolerance for canonical GT (schema v1.2)
LATTICE_TOLERANCE = 0.010      # D2: 1% relative
ANGLE_TOLERANCE = 1.0          # D2: 1 degree


def parse_relax_output(relax_out_path: Path) -> dict:
    """Parse a pw.x vc-relax .out file.

    Returns a dict with:
        status: one of "OK", "UNCONVERGED", "SCF_NOT_CONVERGED", "CRASHED"
        final_total_energy_ry: float | None
        final_pressure_kbar: float | None
        n_ionic_steps: int | None
        wall_time_sec: float | None (if "PWSCF WALL" line present)
    """
    path = Path(relax_out_path)
    if not path.exists():
        return {
            "status": "CRASHED",
            "error": f"file not found: {path}",
            "final_total_energy_ry": None,
            "final_pressure_kbar": None,
            "n_ionic_steps": None,
            "wall_time_sec": None,
        }

    text = path.read_text(encoding="utf-8", errors="ignore")

    # Determine status
    if "convergence NOT achieved" in text:
        status = "SCF_NOT_CONVERGED"
    elif "The maximum number of steps has been reached" in text:
        status = "UNCONVERGED"
    elif "JOB DONE." in text and "bfgs converged" in text:
        status = "OK"
    elif "JOB DONE." in text:
        # Completed but not converged (rare); still treat as OK for data extraction
        status = "OK"
    else:
        status = "CRASHED"

    # Final total energy: last "!    total energy" line
    energies = re.findall(r"!\s+total energy\s*=\s*(-?\d+\.\d+)\s*Ry", text)
    final_E = float(energies[-1]) if energies else None

    # Final pressure: last "P= <value>" in a "total   stress" block
    pressures = re.findall(r"total\s+stress.*?P=\s*(-?\d+\.\d+)", text, flags=re.DOTALL)
    final_P = float(pressures[-1]) if pressures else None

    # n_ionic_steps: "bfgs converged in ... N bfgs steps" or count "BFGS Geometry Optimization" blocks
    m = re.search(r"bfgs converged in\s+\d+\s+scf cycles and\s+(\d+)\s+bfgs steps", text)
    n_ionic = int(m.group(1)) if m else None

    # Wall time: "PWSCF WALL ... h ... m ... s" or similar
    m2 = re.search(r"PWSCF\s*:.*?(\d+\.\d+)s\s+WALL", text, flags=re.DOTALL)
    wall_time = float(m2.group(1)) if m2 else None

    return {
        "status": status,
        "final_total_energy_ry": final_E,
        "final_pressure_kbar": final_P,
        "n_ionic_steps": n_ionic,
        "wall_time_sec": wall_time,
    }


def _delta_e_meV_per_atom(e1_ry: float, e2_ry: float, nat: int) -> float:
    """|ΔE| in meV/atom between two runs of same material."""
    return abs(e2_ry - e1_ry) * RY_TO_MEV / nat


def _max_lattice_drift(r_prev: dict, r_last: dict) -> float:
    """Max of |Δa|, |Δb|, |Δc| over whichever lattice axes are present (Å).

    For non-cubic materials (hex/tet/ortho/monoclinic/triclinic) the c (and
    sometimes b) axis can be the loose axis (e.g. layered h-BN: a is rock-stable
    while c oscillates strongly with cutoff). Using only |Δa| would falsely
    classify these as converged. This helper returns the worst-case drift.

    NOTE: Plan B 判据已切到相对差 `_max_lattice_drift_relative`（杨博 0427）。
    本函数仍保留：(1) 旧报告/审计仍展示绝对 Å 数值；(2) `convergence_evidence` 同时记录两套数值。
    """
    drifts = []
    for axis in ("a", "b", "c"):
        key = f"final_lattice_{axis}"
        v_prev = r_prev.get(key)
        v_last = r_last.get(key)
        if v_prev is not None and v_last is not None:
            drifts.append(abs(v_last - v_prev))
    return max(drifts) if drifts else 0.0


def _max_lattice_drift_relative(r_prev: dict, r_last: dict) -> float:
    """Max relative drift of lattice axes (杨博 0427 判据).

    Relative = |Δa|/|a_last| (and same for b, c); use last-point's value as
    reference (not penultimate) so the metric is scale-invariant of the choice
    of which side of the pair we anchor.

    Returns 0.0 when no axes are available; uses last-point's value to normalize.
    """
    drifts = []
    for axis in ("a", "b", "c"):
        key = f"final_lattice_{axis}"
        v_prev = r_prev.get(key)
        v_last = r_last.get(key)
        if v_prev is not None and v_last is not None and v_last:
            drifts.append(abs(v_last - v_prev) / abs(v_last))
    return max(drifts) if drifts else 0.0


def _round_convergence(results: list[dict]) -> dict:
    """Analyze one scan round (either ecutwfc or kmesh). Returns:
        {
          "converged": bool,
          "delta_E_meV_per_atom_last": float | None,
          "delta_P_kbar_last": float | None,
          "delta_lattice_max_angstrom_last": float | None,   # max(|Δa|,|Δb|,|Δc|) Å (审计用)
          "delta_lattice_max_relative_last": float | None,    # max(|Δa|/|a|, ..) (Plan B 判据)
          "last_params": dict (最后一个 scan point 的参数),
        }
    """
    if len(results) < 2 or any(r["status"] != "OK" for r in results):
        return {
            "converged": False,
            "delta_E_meV_per_atom_last": None,
            "delta_P_kbar_last": None,
            "delta_lattice_max_angstrom_last": None,
            "delta_lattice_max_relative_last": None,
            "last_params": results[-1] if results else {},
        }
    r_prev, r_last = results[-2], results[-1]
    nat = r_last.get("nat", 1)
    dE = _delta_e_meV_per_atom(
        r_prev["final_total_energy_ry"],
        r_last["final_total_energy_ry"],
        nat,
    )
    dP = abs(r_last["final_pressure_kbar"] - r_prev["final_pressure_kbar"])
    dl_aa = _max_lattice_drift(r_prev, r_last)
    dl_rel = _max_lattice_drift_relative(r_prev, r_last)
    converged = (dE < DELTA_E_MEV_PER_ATOM_THR) and (dP < DELTA_P_KBAR_THR)
    return {
        "converged": converged,
        "delta_E_meV_per_atom_last": dE,
        "delta_P_kbar_last": dP,
        "delta_lattice_max_angstrom_last": dl_aa,
        "delta_lattice_max_relative_last": dl_rel,
        "last_params": r_last,
    }


def find_convergence(ecut_results: list[dict], kmesh_results: list[dict]) -> dict:
    """Determine overall convergence status for one material from its 2 scan rounds.

    Args:
        ecut_results: list of parsed scan point results for round 1 (ecutwfc scan),
                      ordered by ascending ecutwfc.
        kmesh_results: list for round 2 (kmesh scan), ordered by ascending density.

    Returns:
        dict with:
            status: "converged" | "marginal" | "not_converged" | "failed"
            ecutwfc_conv: float | None
            kmesh_conv_density: float | None
            delta_lattice_max_relative: float | None   (max of two rounds, max over a/b/c, 相对量, 用于判分)
            delta_lattice_max_angstrom: float | None   (同上但绝对 Å, 审计用)
            ecutwfc_round: sub-dict (see _round_convergence)
            kmesh_round: sub-dict
    """
    # Check for any failed scan point
    all_points = ecut_results + kmesh_results
    if any(r["status"] != "OK" for r in all_points):
        return {
            "status": "failed",
            "ecutwfc_conv": None,
            "kmesh_conv_density": None,
            "delta_lattice_max_relative": None,
            "delta_lattice_max_angstrom": None,
            "failed_points": [r["scan_point"] for r in all_points if r["status"] != "OK"],
            "ecutwfc_round": None,
            "kmesh_round": None,
        }

    e_round = _round_convergence(ecut_results)
    k_round = _round_convergence(kmesh_results)

    # Plan B 收敛判据：几何相对差 ≥ DELTA_L_REL_CONVERGED 即不算 converged.
    # 杨博 0427 拍板：1% 已足够（DFT 间晶格常数本征不确定度本来就在 1%；与判分容差同档）。
    # ΔE / ΔP 仍写进 evidence 块（审计），不再参与分类。max over a/b/c 不变（h-BN c 轴的考虑）。
    delta_lattice_rel_total = max(
        e_round["delta_lattice_max_relative_last"] or 0.0,
        k_round["delta_lattice_max_relative_last"] or 0.0,
    )
    delta_lattice_aa_total = max(
        e_round["delta_lattice_max_angstrom_last"] or 0.0,
        k_round["delta_lattice_max_angstrom_last"] or 0.0,
    )
    if delta_lattice_rel_total < DELTA_L_REL_CONVERGED:
        overall = "converged"
    elif delta_lattice_rel_total < DELTA_L_REL_MARGINAL:
        overall = "marginal"
    else:
        overall = "not_converged"

    return {
        "status": overall,
        "ecutwfc_conv": e_round["last_params"].get("ecutwfc"),
        "kmesh_conv_density": k_round["last_params"].get("kmesh_density"),
        "delta_lattice_max_relative": delta_lattice_rel_total,
        "delta_lattice_max_angstrom": delta_lattice_aa_total,
        "ecutwfc_round": e_round,
        "kmesh_round": k_round,
    }


def _scan_to_evidence_block(results: list[dict], axis: str) -> dict:
    """Convert scan-point results to schema v1.2 ecutwfc_scan/kmesh_scan object.

    axis: 'ecutwfc' or 'kmesh_density'
    """
    if axis == "ecutwfc":
        # values_ry, kmesh_fixed
        kmesh_fixed = results[0].get("kmesh", None) if results else None
        vals = [r["ecutwfc"] for r in results]
        # delta from last point
        last_E = results[-1]["final_total_energy_ry"] if results else 0.0
        last_P = results[-1]["final_pressure_kbar"] if results else 0.0
        nat = results[0].get("nat", 1) if results else 1
        dEs = [_delta_e_meV_per_atom(r["final_total_energy_ry"], last_E, nat) for r in results]
        dPs = [abs(r["final_pressure_kbar"] - last_P) for r in results]
        return {
            "values_ry": vals,
            "kmesh_fixed": kmesh_fixed,
            "delta_E_meV_per_atom": dEs,
            "delta_P_kbar": dPs,
        }
    else:
        kmesh_list = [r["kmesh"] for r in results]
        ecutwfc_fixed = results[0]["ecutwfc"] if results else None
        last_E = results[-1]["final_total_energy_ry"] if results else 0.0
        last_P = results[-1]["final_pressure_kbar"] if results else 0.0
        nat = results[0].get("nat", 1) if results else 1
        dEs = [_delta_e_meV_per_atom(r["final_total_energy_ry"], last_E, nat) for r in results]
        dPs = [abs(r["final_pressure_kbar"] - last_P) for r in results]
        return {
            "values": kmesh_list,
            "ecutwfc_fixed_ry": ecutwfc_fixed,
            "delta_E_meV_per_atom": dEs,
            "delta_P_kbar": dPs,
        }


def build_canonical_gt(
    bench_json: dict,
    final_scan_result: dict,
    convergence: dict,
    ecut_results: list[dict],
    kmesh_results: list[dict],
    total_wall_time_sec: float,
    workdir_rel: str,
) -> dict:
    """Assemble schema v1.2 compliant canonical GT JSON for one material.

    Args:
        bench_json: the original bench JSON (for id / schema_version)
        final_scan_result: the scan point result used as canonical (typically e80_d50)
        convergence: output of find_convergence()
        ecut_results: round 1 scan point results (5 points)
        kmesh_results: round 2 scan point results (4 points; last 3 non-overlapping + shared)
        total_wall_time_sec: sum of all scan points' wall times
        workdir_rel: relative path to workdir for this material (for raw_outputs_dir)

    Returns:
        JSON dict ready to dump to canonical_gt/<id>.json.
    """
    fsr = final_scan_result
    ground_truth = {
        "a": {"value": fsr["final_lattice_a"], "unit": "angstrom",
              "tolerance": LATTICE_TOLERANCE, "rule": "relative"},
        "b": {"value": fsr["final_lattice_b"], "unit": "angstrom",
              "tolerance": LATTICE_TOLERANCE, "rule": "relative"},
        "c": {"value": fsr["final_lattice_c"], "unit": "angstrom",
              "tolerance": LATTICE_TOLERANCE, "rule": "relative"},
        "alpha": {"value": fsr["final_alpha"], "unit": "degree",
                 "tolerance": ANGLE_TOLERANCE, "rule": "absolute"},
        "beta": {"value": fsr["final_beta"], "unit": "degree",
                "tolerance": ANGLE_TOLERANCE, "rule": "absolute"},
        "gamma": {"value": fsr["final_gamma"], "unit": "degree",
                 "tolerance": ANGLE_TOLERANCE, "rule": "absolute"},
        "space_group_number": {"value": fsr["space_group_number"], "rule": "exact"},
        "point_group": {"value": fsr["point_group"], "rule": "exact"},
        "crystal_system": {"value": fsr["crystal_system"], "rule": "exact"},
    }

    conv_ev = {
        "methodology": (
            "Two-round convergence scan: Round 1 fixes kmesh density at 30 and scans ecutwfc "
            "in {30,40,50,60,80}; Round 2 fixes ecutwfc at 80 and scans kmesh density in {20,30,40,50}. "
            "Convergence criteria: ΔE < 0.05 meV/atom and ΔP < 0.5 kbar."
        ),
        "ecutwfc_scan": _scan_to_evidence_block(ecut_results, "ecutwfc"),
        "kmesh_scan": _scan_to_evidence_block(kmesh_results, "kmesh_density"),
        "converged_params": {
            "ecutwfc_ry": fsr["ecutwfc"],
            "ecutrho_ry": fsr["ecutrho"],
            "kmesh": fsr["kmesh"],
            "kmesh_density_pts_per_inv_angstrom": fsr.get("kmesh_density"),
        },
        "convergence_threshold": {
            "delta_E_meV_per_atom": DELTA_E_MEV_PER_ATOM_THR,
            "delta_P_kbar": DELTA_P_KBAR_THR,
            "delta_l_relative_converged": DELTA_L_REL_CONVERGED,
            "delta_l_relative_marginal": DELTA_L_REL_MARGINAL,
            "criterion": "geometry-relative (Plan B, 杨博 0427)",
        },
        "raw_outputs_dir": workdir_rel,
    }

    self_run_meta = {
        "qe_version": "7.5CN-GPU",
        "pseudo_family": "GBRV_USPP_PBE_v1.5",
        "total_runs": len(ecut_results) + len(kmesh_results) - 1,  # overlap skipped
        "wall_time_sec_total": total_wall_time_sec,
        "convergence_status": convergence["status"],
        "delta_lattice_max_relative": convergence.get("delta_lattice_max_relative"),
        "delta_lattice_max_angstrom": convergence["delta_lattice_max_angstrom"],
    }

    return {
        "id": bench_json["id"],
        "schema_version": bench_json.get("schema_version", "1.2"),
        "ground_truth": ground_truth,
        "convergence_evidence": conv_ev,
        "self_run_meta": self_run_meta,
    }


def extract_geometry_from_relax_out(
    relax_out_path: Path,
    species: list[str] | None = None,
) -> dict:
    """Extract final geometry + symmetry info from pw.x vc-relax output.

    Args:
        relax_out_path: path to relax.out
        species: list of element symbols expected in ATOMIC_POSITIONS (for validation)

    Returns:
        dict with: a, b, c (Å), alpha, beta, gamma (°), space_group_number,
                   point_group, crystal_system, nat

    Note on 'a': when CELL_PARAMETERS unit is 'alat', 'a' is returned as
    alat_bohr * BOHR_TO_ANG (the QE lattice scale parameter), which equals the
    conventional lattice constant for cubic systems. 'b' and 'c' are lat.abc[1:].
    For 'angstrom' or 'bohr' units, a, b, c = lat.abc.
    """
    text = Path(relax_out_path).read_text(encoding="utf-8", errors="ignore")

    # Find "Begin final coordinates" block and parse CELL_PARAMETERS + ATOMIC_POSITIONS.
    block_match = re.search(r"Begin final coordinates(.*?)End final coordinates", text, re.DOTALL)
    if not block_match:
        raise ValueError(f"no 'Begin final coordinates' block in {relax_out_path}")
    block = block_match.group(1)

    # CELL_PARAMETERS (alat=N) / (angstrom) / (bohr)
    # Use [^\n]+ (not [-\d.\s]+) for rows to prevent greedy cross-line matching.
    cell_match = re.search(
        r"CELL_PARAMETERS\s*\(\s*(alat\s*=\s*([0-9.]+)|angstrom|bohr)\s*\)\s*\n"
        r"([^\n]+)\n([^\n]+)\n([^\n]+)",
        block,
    )
    if not cell_match:
        raise ValueError(f"no CELL_PARAMETERS in {relax_out_path}")
    unit_spec = cell_match.group(1).strip()
    rows = [
        [float(x) for x in cell_match.group(i).split()]
        for i in (3, 4, 5)
    ]
    if unit_spec.startswith("alat"):
        alat_bohr = float(cell_match.group(2))
        scale = alat_bohr * BOHR_TO_ANG
    elif unit_spec == "bohr":
        alat_bohr = None
        scale = BOHR_TO_ANG
    else:
        alat_bohr = None
        scale = 1.0  # angstrom
    matrix = [[v * scale for v in row] for row in rows]

    # ATOMIC_POSITIONS (crystal|angstrom|bohr)
    pos_match = re.search(
        r"ATOMIC_POSITIONS\s*\(\s*(crystal|angstrom|bohr)\s*\)\s*\n"
        r"((?:\s*\S+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s*\n)+)",
        block,
    )
    if not pos_match:
        raise ValueError(f"no ATOMIC_POSITIONS in {relax_out_path}")
    pos_unit = pos_match.group(1)
    pos_lines = [ln for ln in pos_match.group(2).strip().split("\n") if ln.strip()]
    species_list: list[str] = []
    coords: list[list[float]] = []
    for ln in pos_lines:
        parts = ln.split()
        species_list.append(parts[0])
        coords.append([float(p) for p in parts[1:4]])

    if species is not None:
        for el in set(species_list):
            if el not in species:
                raise ValueError(f"unexpected element {el} (expected {species})")

    # Build pymatgen Structure and run SpacegroupAnalyzer.
    lat = Lattice(matrix)
    coords_are_cartesian = (pos_unit != "crystal")
    struct = Structure(lat, species_list, coords, coords_are_cartesian=coords_are_cartesian)
    sga = SpacegroupAnalyzer(struct, symprec=1e-3, angle_tolerance=5.0)

    # Return lattice parameters.
    # For 'alat' unit: 'a' = alat_bohr * BOHR_TO_ANG (the QE lattice scale / conventional a).
    # For 'angstrom' / 'bohr' units: use lat.abc directly.
    _, b_lat, c_lat = lat.abc
    _, beta, gamma = lat.angles
    alpha = lat.angles[0]
    if alat_bohr is not None:
        a_out = alat_bohr * BOHR_TO_ANG
    else:
        a_out = lat.abc[0]

    return {
        "a": a_out,
        "b": b_lat,
        "c": c_lat,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "space_group_number": sga.get_space_group_number(),
        "point_group": sga.get_point_group_symbol(),
        "crystal_system": sga.get_crystal_system(),
        "nat": len(species_list),
    }


# ------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
WORKDIR_ROOT = _HERE / "workdir"
CANONICAL_GT_DIR = _HERE / "canonical_gt"


def load_material_scan_results(material_workdir: Path) -> tuple[list[dict], list[dict]]:
    """Load all scan point result.json files for one material.

    Returns:
        (ecut_results, kmesh_results) sorted by ascending ecutwfc / kmesh_density.
        The (ecutwfc=80, density=30) overlap point appears in both lists.
    """
    import json as _json
    all_results: list[dict] = []
    for subdir in material_workdir.iterdir():
        result_file = subdir / "result.json"
        if result_file.exists():
            all_results.append(_json.loads(result_file.read_text()))
    # round=1 → ecut scan; round=2 → kmesh scan
    ecut = sorted((r for r in all_results if r.get("round") == 1),
                  key=lambda r: r["ecutwfc"])
    kmesh = sorted((r for r in all_results if r.get("round") == 2),
                   key=lambda r: r["kmesh_density"])
    # Include the round-1 e80_d30 point as kmesh's density=30 anchor (for kmesh_scan table)
    # Find round-1 point with ecutwfc=80
    overlap = next((r for r in ecut if r["ecutwfc"] == 80.0), None)
    if overlap is not None:
        kmesh_merged = sorted(kmesh + [overlap], key=lambda r: r["kmesh_density"])
    else:
        kmesh_merged = kmesh
    return ecut, kmesh_merged


def aggregate_material(material_id: str, bench_json_path: Path) -> dict:
    """Aggregate one material's scan results into a canonical GT JSON."""
    import json as _json
    material_workdir = WORKDIR_ROOT / material_id
    if not material_workdir.exists():
        return {
            "id": material_id,
            "status": "failed",
            "error": f"workdir not found: {material_workdir}",
        }

    ecut_results, kmesh_results = load_material_scan_results(material_workdir)
    # 2026-05-13: 新 T1 grid 6 ecut + 4 kmesh (含 e80 overlap)，门槛改为 6/4
    if len(ecut_results) < 6 or len(kmesh_results) < 4:
        return {
            "id": material_id,
            "status": "failed",
            "error": f"incomplete scan results: {len(ecut_results)} ecut, {len(kmesh_results)} kmesh",
        }

    convergence = find_convergence(ecut_results, kmesh_results)

    # Use (ecutwfc=80, kmesh_density=50) as canonical final point
    final = next((r for r in kmesh_results
                  if r["ecutwfc"] == 80.0 and r["kmesh_density"] == 50.0), None)
    if final is None or final["status"] != "OK":
        return {
            "id": material_id,
            "status": "failed",
            "error": "canonical final point (e80_d50) not OK",
            "convergence": convergence,
        }

    bench_json = _json.loads(bench_json_path.read_text())
    total_wall = sum(r.get("wall_time_sec", 0) for r in ecut_results + kmesh_results)
    workdir_rel = str(material_workdir.relative_to(_HERE.parent.parent))  # relative to repo root

    return build_canonical_gt(
        bench_json=bench_json,
        final_scan_result=final,
        convergence=convergence,
        ecut_results=ecut_results,
        kmesh_results=kmesh_results,
        total_wall_time_sec=total_wall,
        workdir_rel=workdir_rel,
    )


def main() -> int:
    """Scan all workdir entries, aggregate to canonical_gt/."""
    import json as _json
    import sys as _sys

    CANONICAL_GT_DIR.mkdir(parents=True, exist_ok=True)
    bench_questions_dir = _HERE.parent / "questions"

    n_ok = 0
    n_failed = 0
    for material_dir in sorted(WORKDIR_ROOT.iterdir()):
        if not material_dir.is_dir():
            continue
        material_id = material_dir.name
        # Find sister bench JSON
        bench_path = None
        candidate = bench_questions_dir / "M1_T1" / f"{material_id}.json"
        if candidate.exists():
            bench_path = candidate
        if bench_path is None:
            print(f"[analyze] SKIP {material_id}: no sister bench JSON", file=_sys.stderr)
            continue

        canonical = aggregate_material(material_id, bench_path)
        out_path = CANONICAL_GT_DIR / f"{material_id}.json"
        out_path.write_text(_json.dumps(canonical, indent=2), encoding="utf-8")

        if canonical.get("status") == "failed":
            n_failed += 1
            print(f"[analyze] FAIL {material_id}: {canonical.get('error')}", file=_sys.stderr)
        else:
            n_ok += 1
            conv_status = canonical.get("self_run_meta", {}).get("convergence_status", "?")
            print(f"[analyze] OK   {material_id}: {conv_status}")

    print(f"\n[analyze] wrote {n_ok} canonical GT JSONs; {n_failed} failed.")
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())

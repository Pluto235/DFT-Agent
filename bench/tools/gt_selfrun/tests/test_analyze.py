"""Unit tests for analyze.py."""
from pathlib import Path

import pytest

from tools.gt_selfrun import analyze

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def test_parse_relax_converged():
    """Converged fixture → status OK, final_E ≈ -16.03892150, n_ionic 4."""
    result = analyze.parse_relax_output(FIXTURES / "sample_relax_converged.out")
    assert result["status"] == "OK"
    assert result["final_total_energy_ry"] == pytest.approx(-16.03892150, abs=1e-6)
    assert result["final_pressure_kbar"] == pytest.approx(0.08, abs=1e-3)
    assert result["n_ionic_steps"] == 4


def test_parse_relax_unconverged():
    """Max steps reached → status UNCONVERGED."""
    result = analyze.parse_relax_output(FIXTURES / "sample_relax_unconverged.out")
    assert result["status"] == "UNCONVERGED"
    # Should still extract last total energy
    assert result["final_total_energy_ry"] == pytest.approx(-15.02120115, abs=1e-6)


def test_parse_relax_scf_crashed():
    """SCF not converged → status SCF_NOT_CONVERGED."""
    result = analyze.parse_relax_output(FIXTURES / "sample_relax_crashed.out")
    assert result["status"] == "SCF_NOT_CONVERGED"


def test_parse_relax_missing_file():
    """Nonexistent file → status CRASHED."""
    result = analyze.parse_relax_output(FIXTURES / "does_not_exist.out")
    assert result["status"] == "CRASHED"


def test_find_convergence_all_converged():
    """Flat scan → ΔE and ΔP both below threshold → status converged."""
    # ecutwfc round: energies monotonically decreasing toward limit
    ecut_results = [
        {"scan_point": "e30_d30", "ecutwfc": 30.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.030, "final_pressure_kbar": 0.5,
         "final_lattice_a": 3.850, "status": "OK", "nat": 2},
        {"scan_point": "e40_d30", "ecutwfc": 40.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.038, "final_pressure_kbar": 0.2,
         "final_lattice_a": 3.849, "status": "OK", "nat": 2},
        {"scan_point": "e50_d30", "ecutwfc": 50.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.0389, "final_pressure_kbar": 0.15,
         "final_lattice_a": 3.849, "status": "OK", "nat": 2},
        {"scan_point": "e60_d30", "ecutwfc": 60.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.03892, "final_pressure_kbar": 0.10,
         "final_lattice_a": 3.8489, "status": "OK", "nat": 2},
        {"scan_point": "e80_d30", "ecutwfc": 80.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.038921, "final_pressure_kbar": 0.08,
         "final_lattice_a": 3.8489, "status": "OK", "nat": 2},
    ]
    # kmesh round: similar
    kmesh_results = [
        {"scan_point": "e80_d20", "ecutwfc": 80.0, "kmesh_density": 20.0,
         "final_total_energy_ry": -16.0380, "final_pressure_kbar": 0.3,
         "final_lattice_a": 3.8495, "status": "OK", "nat": 2},
        {"scan_point": "e80_d30", "ecutwfc": 80.0, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.038921, "final_pressure_kbar": 0.08,
         "final_lattice_a": 3.8489, "status": "OK", "nat": 2},
        {"scan_point": "e80_d40", "ecutwfc": 80.0, "kmesh_density": 40.0,
         "final_total_energy_ry": -16.038935, "final_pressure_kbar": 0.05,
         "final_lattice_a": 3.8489, "status": "OK", "nat": 2},
        {"scan_point": "e80_d50", "ecutwfc": 80.0, "kmesh_density": 50.0,
         "final_total_energy_ry": -16.038938, "final_pressure_kbar": 0.03,
         "final_lattice_a": 3.8489, "status": "OK", "nat": 2},
    ]
    report = analyze.find_convergence(ecut_results, kmesh_results)
    assert report["status"] == "converged"
    assert report["ecutwfc_conv"] == 80.0  # or 60.0, 但 last point is 80
    assert report["delta_lattice_max_angstrom"] < 0.001


def test_find_convergence_lattice_not_converged():
    """Plan B（杨博 0427 切到相对差）: Δl_rel 最后两点 ≥ 2% → not_converged.
    Here last two ecut points jump |Δa|=0.060 Å on a≈3.85 → 1.56% which is
    between converged (1%) and not_converged (2%) → marginal under new rules.
    The not_converged test below uses |Δa|=0.10 (~2.6%) to trigger ≥ 2%."""
    ecut_results = [
        {"scan_point": "e30_d30", "ecutwfc": 30.0, "final_total_energy_ry": -16.000,
         "final_pressure_kbar": 5.0, "final_lattice_a": 3.860, "status": "OK", "nat": 2,
         "kmesh_density": 30.0},
        {"scan_point": "e40_d30", "ecutwfc": 40.0, "final_total_energy_ry": -16.010,
         "final_pressure_kbar": 3.0, "final_lattice_a": 3.855, "status": "OK", "nat": 2,
         "kmesh_density": 30.0},
        {"scan_point": "e50_d30", "ecutwfc": 50.0, "final_total_energy_ry": -16.020,
         "final_pressure_kbar": 2.0, "final_lattice_a": 3.852, "status": "OK", "nat": 2,
         "kmesh_density": 30.0},
        {"scan_point": "e60_d30", "ecutwfc": 60.0, "final_total_energy_ry": -16.030,
         "final_pressure_kbar": 1.5, "final_lattice_a": 3.950, "status": "OK", "nat": 2,
         "kmesh_density": 30.0},
        # Last two: |Δa|=0.10 Å on 3.85 → 2.6% > DELTA_L_REL_MARGINAL (2%) → not_converged
        {"scan_point": "e80_d30", "ecutwfc": 80.0, "final_total_energy_ry": -16.040,
         "final_pressure_kbar": 1.0, "final_lattice_a": 3.850, "status": "OK", "nat": 2,
         "kmesh_density": 30.0},
    ]
    kmesh_results = ecut_results[-1:] + [
        {"scan_point": "e80_d40", "ecutwfc": 80.0, "final_total_energy_ry": -16.040001,
         "final_pressure_kbar": 0.9, "final_lattice_a": 3.850, "status": "OK", "nat": 2,
         "kmesh_density": 40.0},
        {"scan_point": "e80_d50", "ecutwfc": 80.0, "final_total_energy_ry": -16.040002,
         "final_pressure_kbar": 0.85, "final_lattice_a": 3.850, "status": "OK", "nat": 2,
         "kmesh_density": 50.0},
    ]
    report = analyze.find_convergence(ecut_results[:5], kmesh_results)
    assert report["status"] == "not_converged"
    # Both metrics present
    assert report["delta_lattice_max_relative"] >= 0.02
    assert report["delta_lattice_max_angstrom"] >= 0.05


def test_find_convergence_relative_marginal():
    """Δl_rel 在 [1%, 2%) → marginal."""
    ecut_results = [
        {"scan_point": f"e{e}_d30", "ecutwfc": float(e), "kmesh_density": 30.0,
         "final_total_energy_ry": -16.0 - i * 0.01, "final_pressure_kbar": 0.1,
         "final_lattice_a": 3.85 + (0.05 if i == 3 else 0.0),  # last two: 3.90 → 3.85, |Δ|=0.05/3.85 ≈ 1.3%
         "status": "OK", "nat": 2}
        for i, e in enumerate([30, 40, 50, 60, 80])
    ]
    kmesh_results = [ecut_results[-1]] + [
        {"scan_point": f"e80_d{d}", "ecutwfc": 80.0, "kmesh_density": float(d),
         "final_total_energy_ry": -16.04 - i * 0.0001, "final_pressure_kbar": 0.05,
         "final_lattice_a": 3.85, "status": "OK", "nat": 2}
        for i, d in enumerate([40, 50])
    ]
    report = analyze.find_convergence(ecut_results, kmesh_results)
    assert report["status"] == "marginal", (
        f"expected marginal, got {report['status']} "
        f"(Δl_rel={report['delta_lattice_max_relative']})")
    assert 0.01 <= report["delta_lattice_max_relative"] < 0.02


def test_find_convergence_relative_boundary_below_one_percent():
    """|Δa|/|a| 严格 < 1% → converged（边界刚好不算 marginal）。"""
    # |Δa|=0.0384/3.85 ≈ 0.998% < 1%
    ecut_results = [
        {"scan_point": f"e{e}_d30", "ecutwfc": float(e), "kmesh_density": 30.0,
         "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.05,
         "final_lattice_a": 3.85 + (0.0384 if i == 3 else 0.0),
         "status": "OK", "nat": 2}
        for i, e in enumerate([30, 40, 50, 60, 80])
    ]
    kmesh_results = [ecut_results[-1]] + [
        {"scan_point": f"e80_d{d}", "ecutwfc": 80.0, "kmesh_density": float(d),
         "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.05,
         "final_lattice_a": 3.85, "status": "OK", "nat": 2}
        for d in [40, 50]
    ]
    report = analyze.find_convergence(ecut_results, kmesh_results)
    assert report["status"] == "converged"
    assert report["delta_lattice_max_relative"] < 0.01


def test_find_convergence_relative_uses_max_axis():
    """Anisotropic: c-axis drifts 5%, a-axis stable → not_converged from c."""
    ecut_results = [
        {"scan_point": f"e{e}_d30", "ecutwfc": float(e), "kmesh_density": 30.0,
         "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.05,
         "final_lattice_a": 2.51, "final_lattice_b": 2.51,
         "final_lattice_c": 7.0 + (0.5 if i == 3 else 0.0),  # last: 7.5 → 7.0, 7.1%
         "status": "OK", "nat": 4}
        for i, e in enumerate([30, 40, 50, 60, 80])
    ]
    kmesh_results = [ecut_results[-1]] + [
        {"scan_point": f"e80_d{d}", "ecutwfc": 80.0, "kmesh_density": float(d),
         "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.05,
         "final_lattice_a": 2.51, "final_lattice_b": 2.51, "final_lattice_c": 7.0,
         "status": "OK", "nat": 4}
        for d in [40, 50]
    ]
    report = analyze.find_convergence(ecut_results, kmesh_results)
    assert report["status"] == "not_converged"
    assert report["delta_lattice_max_relative"] >= 0.02


def test_find_convergence_with_failed_scan_point():
    """Any scan point with status != OK → overall status 'failed'."""
    bad = [
        {"scan_point": "e30_d30", "ecutwfc": 30.0, "status": "CRASHED",
         "kmesh_density": 30.0, "final_total_energy_ry": None,
         "final_pressure_kbar": None, "final_lattice_a": None, "nat": 2},
    ]
    report = analyze.find_convergence(bad, [])
    assert report["status"] == "failed"


def test_build_canonical_gt_shape():
    """Output JSON has required top-level keys."""
    bench_json = {
        "id": "M1_T1_Si_vc_relax",
        "schema_version": "1.2",
    }
    final_scan_result = {
        "scan_point": "e80_d50",
        "ecutwfc": 80.0,
        "ecutrho": 800.0,
        "kmesh_density": 50.0,
        "kmesh": [14, 14, 14],
        "final_lattice_a": 3.8489,
        "final_lattice_b": 3.8489,
        "final_lattice_c": 3.8489,
        "final_alpha": 60.0,
        "final_beta": 60.0,
        "final_gamma": 60.0,
        "space_group_number": 227,
        "point_group": "m-3m",
        "crystal_system": "cubic",
        "final_total_energy_ry": -16.038938,
        "final_pressure_kbar": 0.03,
        "nat": 2,
        "status": "OK",
    }
    convergence = {
        "status": "converged",
        "ecutwfc_conv": 80.0,
        "kmesh_conv_density": 50.0,
        "delta_lattice_max_angstrom": 0.0003,
        "ecutwfc_round": {"converged": True, "delta_E_meV_per_atom_last": 0.01,
                         "delta_P_kbar_last": 0.05, "delta_lattice_max_angstrom_last": 0.0001,
                         "last_params": {}},
        "kmesh_round": {"converged": True, "delta_E_meV_per_atom_last": 0.003,
                       "delta_P_kbar_last": 0.02, "delta_lattice_max_angstrom_last": 0.0003,
                       "last_params": {}},
    }
    ecut_scan_results = [
        {"scan_point": f"e{int(e)}_d30", "ecutwfc": e, "kmesh_density": 30.0,
         "final_total_energy_ry": -16.0 - i * 0.01, "final_pressure_kbar": 0.5 - i * 0.1,
         "final_lattice_a": 3.850 - i * 0.0003, "nat": 2, "kmesh": [8, 8, 8],
         "status": "OK"}
        for i, e in enumerate([30.0, 40.0, 50.0, 60.0, 80.0])
    ]
    kmesh_scan_results = [
        {"scan_point": f"e80_d{int(d)}", "ecutwfc": 80.0, "kmesh_density": d,
         "final_total_energy_ry": -16.04 - i * 0.0001, "final_pressure_kbar": 0.1 - i * 0.02,
         "final_lattice_a": 3.849 - i * 0.00005, "nat": 2, "kmesh": [8 + 2*i] * 3,
         "status": "OK"}
        for i, d in enumerate([20.0, 30.0, 40.0, 50.0])
    ]
    out = analyze.build_canonical_gt(
        bench_json=bench_json,
        final_scan_result=final_scan_result,
        convergence=convergence,
        ecut_results=ecut_scan_results,
        kmesh_results=kmesh_scan_results,
        total_wall_time_sec=2834.5,
        workdir_rel="tools/gt_selfrun/workdir/M1_T1_Si_vc_relax/",
    )
    assert out["id"] == "M1_T1_Si_vc_relax"
    assert out["schema_version"] == "1.2"
    assert "ground_truth" in out
    assert "convergence_evidence" in out
    assert "self_run_meta" in out


def test_build_canonical_gt_ground_truth_fields():
    """ground_truth has all 9 fields with correct rule + value."""
    bench_json = {"id": "X", "schema_version": "1.2"}
    final = {
        "final_lattice_a": 3.8489, "final_lattice_b": 3.8489, "final_lattice_c": 3.8489,
        "final_alpha": 60.0, "final_beta": 60.0, "final_gamma": 60.0,
        "space_group_number": 227, "point_group": "m-3m", "crystal_system": "cubic",
        "ecutwfc": 80.0, "ecutrho": 800.0, "kmesh_density": 50.0, "kmesh": [14, 14, 14],
        "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.0, "nat": 2, "status": "OK",
        "scan_point": "e80_d50",
    }
    conv = {"status": "converged", "ecutwfc_conv": 80.0, "kmesh_conv_density": 50.0,
            "delta_lattice_max_angstrom": 0.0003,
            "ecutwfc_round": {"converged": True, "delta_E_meV_per_atom_last": 0.01,
                             "delta_P_kbar_last": 0.05, "delta_lattice_max_angstrom_last": 0.0001,
                             "last_params": {}},
            "kmesh_round": {"converged": True, "delta_E_meV_per_atom_last": 0.003,
                           "delta_P_kbar_last": 0.02, "delta_lattice_max_angstrom_last": 0.0003,
                           "last_params": {}}}
    out = analyze.build_canonical_gt(bench_json, final, conv, [], [], 0.0, "")
    gt = out["ground_truth"]
    for key in ("a", "b", "c", "alpha", "beta", "gamma",
                "space_group_number", "point_group", "crystal_system"):
        assert key in gt, f"missing gt.{key}"
    assert gt["a"]["value"] == 3.8489
    assert gt["a"]["rule"] == "relative"
    assert gt["alpha"]["rule"] == "absolute"
    assert gt["space_group_number"]["rule"] == "exact"
    assert gt["space_group_number"]["value"] == 227
    assert gt["point_group"]["value"] == "m-3m"


def test_build_canonical_gt_convergence_evidence():
    """convergence_evidence has the schema-v1.2 required subfields."""
    bench_json = {"id": "X", "schema_version": "1.2"}
    final = {"final_lattice_a": 3.8, "final_lattice_b": 3.8, "final_lattice_c": 3.8,
             "final_alpha": 60.0, "final_beta": 60.0, "final_gamma": 60.0,
             "space_group_number": 227, "point_group": "m-3m", "crystal_system": "cubic",
             "ecutwfc": 80.0, "ecutrho": 800.0, "kmesh_density": 50.0, "kmesh": [14, 14, 14],
             "final_total_energy_ry": -16.0, "final_pressure_kbar": 0.0, "nat": 2,
             "status": "OK", "scan_point": "e80_d50"}
    conv = {"status": "converged", "ecutwfc_conv": 80.0, "kmesh_conv_density": 50.0,
            "delta_lattice_max_angstrom": 0.0003,
            "ecutwfc_round": {"converged": True, "delta_E_meV_per_atom_last": 0.01,
                             "delta_P_kbar_last": 0.05, "delta_lattice_max_angstrom_last": 0.0001,
                             "last_params": {}},
            "kmesh_round": {"converged": True, "delta_E_meV_per_atom_last": 0.003,
                           "delta_P_kbar_last": 0.02, "delta_lattice_max_angstrom_last": 0.0003,
                           "last_params": {}}}
    ecut = [{"scan_point": f"e{e}_d30", "ecutwfc": e, "kmesh_density": 30.0,
             "final_total_energy_ry": -16.0 - i * 0.01, "final_pressure_kbar": 0.1,
             "final_lattice_a": 3.8, "nat": 2, "kmesh": [8, 8, 8], "status": "OK"}
            for i, e in enumerate([30.0, 40.0, 50.0, 60.0, 80.0])]
    kmesh = [{"scan_point": f"e80_d{d}", "ecutwfc": 80.0, "kmesh_density": d,
              "final_total_energy_ry": -16.04, "final_pressure_kbar": 0.05,
              "final_lattice_a": 3.8, "nat": 2, "kmesh": [10, 10, 10], "status": "OK"}
             for d in [20.0, 30.0, 40.0, 50.0]]
    out = analyze.build_canonical_gt(bench_json, final, conv, ecut, kmesh, 0.0, "path")
    ce = out["convergence_evidence"]
    assert "methodology" in ce
    assert "ecutwfc_scan" in ce
    assert "kmesh_scan" in ce
    assert "converged_params" in ce
    assert "convergence_threshold" in ce
    assert ce["converged_params"]["ecutwfc_ry"] == 80.0
    assert ce["convergence_threshold"]["delta_E_meV_per_atom"] == 0.5
    assert len(ce["ecutwfc_scan"]["values_ry"]) == 5
    assert len(ce["kmesh_scan"]["values"]) == 4


def test_extract_geometry_converged():
    """Si fixture: final lattice is Fd-3m-like primitive cell."""
    path = FIXTURES / "sample_relax_converged.out"
    # We need a sister file 'relax.in' to rebuild PWInput; fixture doesn't have it.
    # For this test, provide species list directly.
    geom = analyze.extract_geometry_from_relax_out(path, species=["Si"])
    assert geom["nat"] == 2
    assert geom["a"] == pytest.approx(3.794, abs=0.1)  # Si with alat=7.16 bohr ≈ 3.79 Å
    assert geom["space_group_number"] in (227, 166)  # might parse as either depending on symprec

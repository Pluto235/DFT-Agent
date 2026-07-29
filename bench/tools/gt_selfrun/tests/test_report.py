"""Unit tests for report.py."""
import csv
import json
from pathlib import Path

import pytest

from tools.gt_selfrun import report


def test_compute_comparison_within_d2():
    """Small deltas → within_d2_tolerance = True."""
    mp_gt = {
        "a": 3.849, "b": 3.849, "c": 3.849,
        "alpha": 60.0, "beta": 60.0, "gamma": 60.0,
        "space_group_number": 227,
    }
    self_gt = {
        "a": 3.851, "b": 3.851, "c": 3.851,  # 0.05% diff < 1%
        "alpha": 60.2, "beta": 60.2, "gamma": 60.2,  # 0.2° < 1°
        "space_group_number": 227,
    }
    row = report.compute_comparison(
        material_id="M1_T1_Si_vc_relax", formula="Si",
        tier="M1", category="elemental_semiconductors",
        mp_gt=mp_gt, self_gt=self_gt, convergence_status="converged",
    )
    assert row["within_d2_tolerance"] is True
    assert row["sg_preserved"] is True


def test_compute_comparison_exceeds_d2():
    """Big deltas → within_d2_tolerance = False, recommend_replace = True when converged."""
    mp_gt = {
        "a": 3.849, "b": 3.849, "c": 3.849,
        "alpha": 60.0, "beta": 60.0, "gamma": 60.0,
        "space_group_number": 227,
    }
    self_gt = {
        "a": 3.900, "b": 3.900, "c": 3.900,  # 1.3% > 1%
        "alpha": 60.0, "beta": 60.0, "gamma": 60.0,
        "space_group_number": 227,
    }
    row = report.compute_comparison(
        material_id="X", formula="X", tier="M1", category="c",
        mp_gt=mp_gt, self_gt=self_gt, convergence_status="converged",
    )
    assert row["within_d2_tolerance"] is False
    assert row["recommend_replace_mp_gt"] is True


def test_compute_comparison_not_converged_no_recommendation():
    """not_converged → recommend_replace should be False regardless of delta."""
    mp_gt = {"a": 3.849, "b": 3.849, "c": 3.849, "alpha": 60.0, "beta": 60.0, "gamma": 60.0,
             "space_group_number": 227}
    self_gt = {"a": 3.900, "b": 3.900, "c": 3.900, "alpha": 60.0, "beta": 60.0, "gamma": 60.0,
               "space_group_number": 227}
    row = report.compute_comparison(
        material_id="X", formula="X", tier="M1", category="c",
        mp_gt=mp_gt, self_gt=self_gt, convergence_status="not_converged",
    )
    assert row["recommend_replace_mp_gt"] is False


def test_write_csv_structure(tmp_path):
    """write_csv writes a readable CSV with all expected columns."""
    rows = [
        {"id": "M1_T1_Si_vc_relax", "formula": "Si", "tier": "M1", "category": "esi",
         "overall_status": "OK", "convergence_status": "converged",
         "delta_lattice_max_angstrom": 0.0003,
         "a_mp": 3.849, "a_self": 3.851, "da_abs": 0.002, "da_rel": 0.00052,
         "b_mp": 3.849, "b_self": 3.851, "db_abs": 0.002, "db_rel": 0.00052,
         "c_mp": 3.849, "c_self": 3.851, "dc_abs": 0.002, "dc_rel": 0.00052,
         "alpha_mp": 60.0, "alpha_self": 60.2, "dalpha_abs": 0.2,
         "beta_mp": 60.0, "beta_self": 60.2, "dbeta_abs": 0.2,
         "gamma_mp": 60.0, "gamma_self": 60.2, "dgamma_abs": 0.2,
         "sg_mp": 227, "sg_self": 227, "sg_preserved": True,
         "point_group_mp": "m-3m", "point_group_self": "m-3m",
         "crystal_system_mp": "cubic", "crystal_system_self": "cubic",
         "within_d2_tolerance": True, "recommend_replace_mp_gt": False,
         "ecutwfc_conv": 80.0, "kmesh_conv_density": 50.0},
    ]
    out = tmp_path / "comp.csv"
    report.write_csv(rows, out)
    with open(out) as f:
        reader = csv.DictReader(f)
        actual_rows = list(reader)
    assert len(actual_rows) == 1
    assert actual_rows[0]["id"] == "M1_T1_Si_vc_relax"
    assert actual_rows[0]["formula"] == "Si"

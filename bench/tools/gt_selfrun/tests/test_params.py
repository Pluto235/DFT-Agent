"""Unit tests for params.py."""
import json
from pathlib import Path

import pytest

from tools.gt_selfrun import params

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def test_scan_grid_returns_11_points():
    """Strategy A (extended): 轮 1 (8 ecutwfc @ density=30) + 轮 2 (3 额外 kmesh @ ecutwfc=80),
    overlap at (e80, d30) skipped → 11 distinct scan points."""
    points = params.scan_grid()
    assert len(points) == 11, f"expected 11 scan points, got {len(points)}"


def test_scan_grid_ecutwfc_values():
    """Round 1 sweeps ecutwfc ∈ {30,40,50,60,80,100,120,150} Ry."""
    points = params.scan_grid()
    round1 = [p for p in points if p.round == 1]
    assert len(round1) == 8
    assert sorted(p.ecutwfc for p in round1) == [30.0, 40.0, 50.0, 60.0, 80.0, 100.0, 120.0, 150.0]


def test_scan_grid_kmesh_density_values():
    """Round 2 sweeps kmesh density ∈ {20, 40, 50} pts/Å⁻¹ (30 was covered in round 1)."""
    points = params.scan_grid()
    round2 = [p for p in points if p.round == 2]
    assert len(round2) == 3
    assert sorted(p.kmesh_density for p in round2) == [20.0, 40.0, 50.0]


def test_scan_grid_round2_ecutwfc_fixed():
    """Round 2 uses ecutwfc = 80 Ry (最严)."""
    points = params.scan_grid()
    for p in (p for p in points if p.round == 2):
        assert p.ecutwfc == 80.0


def test_scan_grid_ecutrho_is_10x_ecutwfc():
    """USPP standard: ecutrho = 10 * ecutwfc."""
    for p in params.scan_grid():
        assert p.ecutrho == 10.0 * p.ecutwfc


def test_scan_grid_keys_are_unique():
    """Each scan point has a unique key."""
    points = params.scan_grid()
    keys = [p.key for p in points]
    assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


def test_scan_grid_key_format():
    """Keys are 'e{ecutwfc_int}_d{density_int}' format."""
    for p in params.scan_grid():
        assert p.key.startswith("e") and "_d" in p.key


import numpy as np


def test_kmesh_from_density_cubic_simple():
    """Cubic with a=b=c=4 Å → |b_i| = 2π/4 ≈ 1.571 Å⁻¹.
    At density=30 pts/Å⁻¹: n_i = round(30 * 1.571 / 2π) = round(7.5) = 8 (rounded up to even)."""
    from pymatgen.core import Lattice
    lat = Lattice.cubic(4.0)
    n = params.kmesh_from_density(lat, density=30.0)
    assert n == [8, 8, 8], f"got {n}"


def test_kmesh_from_density_anisotropic():
    """Orthorhombic a=10, b=5, c=20 Å → |b_i| ≈ 0.628, 1.257, 0.314 Å⁻¹."""
    from pymatgen.core import Lattice
    lat = Lattice.from_parameters(10.0, 5.0, 20.0, 90.0, 90.0, 90.0)
    n = params.kmesh_from_density(lat, density=30.0)
    # expected (before even rounding): [3, 6, 2] → even-rounded [4, 6, 2]
    assert n == [4, 6, 2], f"got {n}"


def test_kmesh_from_density_minimum_2():
    """Even if density gives <2, floor to 2."""
    from pymatgen.core import Lattice
    lat = Lattice.cubic(50.0)  # very large cell → |b_i| tiny
    n = params.kmesh_from_density(lat, density=10.0)
    assert all(ni >= 2 for ni in n), f"got {n}"


def test_kmesh_from_density_even_rounding():
    """Round up to even to maintain MP symmetry."""
    from pymatgen.core import Lattice
    # Cubic a=3 Å → |b_i| ≈ 2.094 Å⁻¹
    # At density=30: raw n = 30 * 2.094 / 2π = 9.998 → round = 10 (even, OK)
    # At density=25: raw n = 25 * 2.094 / 2π = 8.332 → round = 8 (even, OK)
    # At density=22: raw n = 22 * 2.094 / 2π = 7.332 → round = 7 (odd) → 8
    lat = Lattice.cubic(3.0)
    n22 = params.kmesh_from_density(lat, density=22.0)
    assert n22 == [8, 8, 8], f"got {n22}"


def test_is_metal_semiconductor(tmp_path):
    """Si is semiconductor → is_metal returns False."""
    # Build a T1 bench JSON fixture + sibling T2 with indirect gap
    t1 = tmp_path / "M1_T1" / "M1_T1_Si_vc_relax.json"
    t2 = tmp_path / "M1_T2" / "M1_T2_Si_bands_dos.json"
    t1.parent.mkdir(parents=True)
    t2.parent.mkdir(parents=True)
    t1.write_text('{"id": "M1_T1_Si_vc_relax", "material": {"formula": "Si"}}')
    t2.write_text(json.dumps({
        "id": "M1_T2_Si_bands_dos",
        "ground_truth": {"gap_type": {"value": "indirect"}}
    }))
    assert params.is_metal(t1) is False


def test_is_metal_metallic(tmp_path):
    """Cu is metal → is_metal returns True."""
    t1 = tmp_path / "M1_T1" / "M1_T1_Cu_vc_relax.json"
    t2 = tmp_path / "M1_T2" / "M1_T2_Cu_bands_dos.json"
    t1.parent.mkdir(parents=True)
    t2.parent.mkdir(parents=True)
    t1.write_text('{"id": "M1_T1_Cu_vc_relax", "material": {"formula": "Cu"}}')
    t2.write_text(json.dumps({
        "id": "M1_T2_Cu_bands_dos",
        "ground_truth": {"gap_type": {"value": "metal"}}
    }))
    assert params.is_metal(t1) is True


def test_is_metal_t2_missing_raises(tmp_path):
    """If sibling T2 JSON doesn't exist, raise explicit error."""
    t1 = tmp_path / "M1_T1" / "M1_T1_X_vc_relax.json"
    t1.parent.mkdir(parents=True)
    t1.write_text('{"id": "M1_T1_X_vc_relax"}')
    with pytest.raises(FileNotFoundError, match="T2 sibling"):
        params.is_metal(t1)


def test_render_qe_input_has_required_blocks(tmp_path):
    """Rendered pw.x input contains all required namelists and cards."""
    # Load Si bench JSON
    bench_json = json.loads((FIXTURES / "sample_si_bench.json").read_text())
    sp = params.ScanPoint(ecutwfc=40.0, ecutrho=400.0, kmesh_density=30.0, round=1)
    kmesh = [8, 8, 8]
    pseudo_dir = tmp_path / "pseudos"
    pseudo_dir.mkdir()
    (pseudo_dir / "Si.upf").touch()

    text = params.render_qe_input(
        bench_json=bench_json,
        scan_point=sp,
        kmesh=kmesh,
        is_metal=False,
        pseudo_dir=pseudo_dir,
    )

    assert "&CONTROL" in text
    assert "calculation = 'vc-relax'" in text
    assert "&SYSTEM" in text
    assert "ecutwfc = 40.0" in text
    assert "ecutrho = 400.0" in text
    assert "&ELECTRONS" in text
    assert "&IONS" in text
    assert "&CELL" in text
    assert "press_conv_thr = 0.5" in text
    assert "ATOMIC_SPECIES" in text
    assert "Si" in text and "Si.upf" in text
    assert "CELL_PARAMETERS (angstrom)" in text
    assert "ATOMIC_POSITIONS (crystal)" in text
    assert "K_POINTS (automatic)" in text
    assert "8 8 8 0 0 0" in text


def test_render_qe_input_metal_has_smearing(tmp_path):
    """Metal rendering has occupations='smearing' and smearing/degauss keys."""
    bench_json = {
        "id": "M1_T1_Al_vc_relax",
        "material": {
            "formula": "Al",
            "initial_structure": {
                "lattice_constants_angstrom": {"a": 2.863, "b": 2.863, "c": 2.863},
                "lattice_angles_degree": {"alpha": 60.0, "beta": 60.0, "gamma": 60.0},
                "atomic_positions_crystal": [{"element": "Al", "position": [0.0, 0.0, 0.0]}],
            },
            "pseudopotential": {"files": {"Al": "Al.upf"}},
        },
    }
    sp = params.ScanPoint(ecutwfc=40.0, ecutrho=400.0, kmesh_density=30.0, round=1)
    pseudo_dir = tmp_path / "pseudos"
    pseudo_dir.mkdir()
    (pseudo_dir / "Al.upf").touch()

    text = params.render_qe_input(
        bench_json=bench_json,
        scan_point=sp,
        kmesh=[10, 10, 10],
        is_metal=True,
        pseudo_dir=pseudo_dir,
    )

    assert "occupations = 'smearing'" in text
    assert "smearing = 'mv'" in text
    assert "degauss = 0.02" in text


def test_render_qe_input_nonmetal_no_smearing(tmp_path):
    """Non-metal rendering omits smearing/degauss."""
    bench_json = json.loads((FIXTURES / "sample_si_bench.json").read_text())
    sp = params.ScanPoint(ecutwfc=40.0, ecutrho=400.0, kmesh_density=30.0, round=1)
    pseudo_dir = tmp_path / "pseudos"
    pseudo_dir.mkdir()
    (pseudo_dir / "Si.upf").touch()

    text = params.render_qe_input(
        bench_json=bench_json,
        scan_point=sp,
        kmesh=[8, 8, 8],
        is_metal=False,
        pseudo_dir=pseudo_dir,
    )

    assert "occupations = 'fixed'" in text
    assert "smearing" not in text
    assert "degauss" not in text

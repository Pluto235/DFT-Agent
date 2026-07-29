"""Test harness.lib.evaluator 用一个最小 fixture QE vc-relax 输出。"""
from pathlib import Path
import pytest
from harness.lib.evaluator import (
    _parse_magnetic_moments,
    extract_t1_fields,
    extract_t2_fields,
    parse_bands_out_eigenvalues,
)


# 最小化 fixture：用 alat = 10.285 bohr (对应 conventional Si a=5.443Å)
# primitive cell FCC vectors (-0.5, 0, 0.5) etc 的长度 = alat * sqrt(0.5)
# = 10.285 * 0.707 bohr = 7.272 bohr = 3.849 Å (primitive rhombohedral a)
SI_VCRELAX_FIXTURE = """
     Program PWSCF v.7.4.1 starts on ...
     ...
     Self-consistent Calculation

     End of self-consistent calculation
     ...
     CELL_PARAMETERS (alat= 10.285)
  -0.500000   0.000000   0.500000
   0.000000   0.500000   0.500000
  -0.500000   0.500000   0.000000

     ATOMIC_POSITIONS (crystal)
Si       0.250000000   0.250000000   0.250000000
Si       0.000000000   0.000000000   0.000000000
     End final coordinates

     JOB DONE.
"""


# M3 风格 fixture：在 Si 结构后塞 nspin=2 的多 SCF iter 磁化打印，
# 末尾一次 = converged 值（17.42 / 17.45），前几次是 BFGS 中间帧。
FE_VCRELAX_FIXTURE = """
     Program PWSCF v.7.4.1 starts on ...
     total magnetization       =    20.00 Bohr mag/cell
     absolute magnetization    =    20.00 Bohr mag/cell
     ...
     total magnetization       =    18.13 Bohr mag/cell
     absolute magnetization    =    18.20 Bohr mag/cell
     ...
     End of self-consistent calculation
     total magnetization       =    17.42 Bohr mag/cell
     absolute magnetization    =    17.45 Bohr mag/cell
     CELL_PARAMETERS (alat= 10.285)
  -0.500000   0.000000   0.500000
   0.000000   0.500000   0.500000
  -0.500000   0.500000   0.000000

     ATOMIC_POSITIONS (crystal)
Si       0.250000000   0.250000000   0.250000000
Si       0.000000000   0.000000000   0.000000000
     End final coordinates

     JOB DONE.
"""


def test_extract_t1_fields_from_fixture(tmp_path):
    fixture = tmp_path / "relax.out"
    fixture.write_text(SI_VCRELAX_FIXTURE)

    result = extract_t1_fields(
        relax_out_path=fixture,
        extraction_config={"symprec": 0.001, "angle_tolerance": 5},
    )

    assert "a" in result
    assert "b" in result
    assert "c" in result
    assert "alpha" in result
    assert "beta" in result
    assert "gamma" in result
    assert result["space_group_number"] == 227  # diamond Fd-3m
    # diamond primitive cell 角 = 60°
    assert abs(result["alpha"] - 60.0) < 0.1
    assert abs(result["beta"] - 60.0) < 0.1
    assert abs(result["gamma"] - 60.0) < 0.1
    # primitive lattice param ~ 3.85 Å
    for k in ["a", "b", "c"]:
        assert 3.5 < result[k] < 4.2, f"{k}={result[k]}"
    # nspin=1 fixture 不含 magnetization 行 → 不进 result（M1/M2 shape 不变）
    assert "total_magnetization_per_cell" not in result
    assert "absolute_magnetization_per_cell" not in result


def test_missing_final_coords_returns_error(tmp_path):
    fixture = tmp_path / "relax.out"
    fixture.write_text("Program PWSCF\nJOB DONE.\n")  # 没有 final 结构

    with pytest.raises(ValueError, match="final|coord|CELL"):
        extract_t1_fields(
            relax_out_path=fixture,
            extraction_config={"symprec": 0.001, "angle_tolerance": 5},
        )


def test_parse_magnetic_moments_picks_last_iter():
    # 多 SCF iter，取最后一次 (converged 值)
    total, absol = _parse_magnetic_moments(FE_VCRELAX_FIXTURE)
    assert total == pytest.approx(17.42)
    assert absol == pytest.approx(17.45)


def test_parse_magnetic_moments_nspin1_returns_none():
    # nspin=1 fixture 无 magnetization 行
    total, absol = _parse_magnetic_moments(SI_VCRELAX_FIXTURE)
    assert total is None
    assert absol is None


def test_extract_t1_fields_includes_magnetization_for_m3(tmp_path):
    fixture = tmp_path / "relax.out"
    fixture.write_text(FE_VCRELAX_FIXTURE)

    result = extract_t1_fields(
        relax_out_path=fixture,
        extraction_config={"symprec": 0.001, "angle_tolerance": 5},
    )

    assert result["total_magnetization_per_cell"] == pytest.approx(17.42)
    assert result["absolute_magnetization_per_cell"] == pytest.approx(17.45)


# ─────────────────────────────────────────────────────────────────────────
# T2 nspin=2 two-spin union (FM-metal spurious-gap bug, 2026-06-05)
#
# Bug: bands.dat from a single bands.x pass holds only the majority (spin-up)
# channel. For an FM metal whose Fermi level is minority-spin-dominated (fcc Ni:
# majority d fully below E_F, minority d straddles E_F), spin-up-only looks like
# a small-gap semiconductor → gap_type mislabeled. Fix: union the spin-down
# eigenvalues parsed from the pw.x bands NSCF output (which has BOTH spins) into
# the per-k band set before gap detection.
# ─────────────────────────────────────────────────────────────────────────

# pw.x bands NSCF output, nspin=2. Spin-up has a 1.5 eV gap straddling E_F=18.0;
# spin-down has a band crossing E_F (18.004 below, 18.006 above) → union → metal.
NSPIN2_BANDS_OUT = """
     Program PWSCF v.7.5 starts on ...
     the Fermi energy is    18.0000 ev

 ------ SPIN UP ------------

          k = 0.0000 0.0000 0.0000 (   100 PWs)   bands (ev):

    16.0000  20.0000

          k = 0.1000 0.0000 0.0000 (   100 PWs)   bands (ev):

    17.5000  20.5000

          k = 0.2000 0.0000 0.0000 (   100 PWs)   bands (ev):

    15.0000  19.0000

 ------ SPIN DOWN ----------

          k = 0.0000 0.0000 0.0000 (   100 PWs)   bands (ev):

    17.0000  18.0040

          k = 0.1000 0.0000 0.0000 (   100 PWs)   bands (ev):

    18.0000  19.0000

          k = 0.2000 0.0000 0.0000 (   100 PWs)   bands (ev):

    16.0000  18.0060

     JOB DONE.
"""

NSPIN1_BANDS_OUT = """
     Program PWSCF v.7.5 starts on ...
     the Fermi energy is    18.0000 ev

          k = 0.0000 0.0000 0.0000 (   100 PWs)   bands (ev):

    16.0000  20.0000

          k = 0.1000 0.0000 0.0000 (   100 PWs)   bands (ev):

    17.5000  20.5000

     JOB DONE.
"""

# spin-up only bands.dat (matches NSPIN2_BANDS_OUT SPIN UP, rounded to 3 dp)
BANDS_DAT_SPINUP = """ &plot nbnd=  2, nks=   3 /
           0.000000  0.000000  0.000000
   16.000  20.000
           0.100000  0.000000  0.000000
   17.500  20.500
           0.200000  0.000000  0.000000
   15.000  19.000
"""

SCF_OUT_MIN = """
     Program PWSCF v.7.5 starts on ...
     bravais-lattice index     =            2
     lattice parameter (alat)  =       6.5677  a.u.
     celldm(1)=   6.567700
     the Fermi energy is    18.0000 ev
     JOB DONE.
"""

DOS_DAT_MIN = """#  E (eV)   dos(E)     Int dos(E) EFermi =   18.000 eV
  17.000     0.100      0.500
  18.000     0.200      0.700
  19.000     0.150      0.900
"""

NSCF_DOS_OUT_MIN = """
     Program PWSCF v.7.5 starts on ...
     the Fermi energy is    18.0000 ev
     JOB DONE.
"""

NI_FCC_STRUCTURE = {
    "lattice_constants_angstrom": {"a": 3.4751, "b": 3.4751, "c": 3.4751},
    "lattice_angles_degree": {"alpha": 90.0, "beta": 90.0, "gamma": 90.0},
    "atomic_positions_crystal": [
        {"element": "Ni", "position": [0.0, 0.0, 0.0]},
        {"element": "Ni", "position": [0.0, 0.5, 0.5]},
        {"element": "Ni", "position": [0.5, 0.0, 0.5]},
        {"element": "Ni", "position": [0.5, 0.5, 0.0]},
    ],
}


def test_parse_bands_out_eigenvalues_nspin2():
    res = parse_bands_out_eigenvalues_from_text(NSPIN2_BANDS_OUT)
    assert res["nspin"] == 2
    assert res["eigs_up"] == [[16.0, 20.0], [17.5, 20.5], [15.0, 19.0]]
    assert res["eigs_dn"] == [[17.0, 18.004], [18.0, 19.0], [16.0, 18.006]]


def test_parse_bands_out_eigenvalues_nspin1():
    res = parse_bands_out_eigenvalues_from_text(NSPIN1_BANDS_OUT)
    assert res["nspin"] == 1
    assert res["eigs_up"] == [[16.0, 20.0], [17.5, 20.5]]
    assert res["eigs_dn"] is None


# Real pw.x outputs often concatenate several reruns (agent retries append to
# the same .out). The parser must use the FINAL run's spin sections, not lump
# every later dump into spin-down. (Observed: Ni_fcc out had 4 SPIN UP/DOWN.)
STALE_PRIOR_RUN = """
     Program PWSCF v.7.5 starts on ...

 ------ SPIN UP ------------

          k = 0.0000 0.0000 0.0000 (   100 PWs)   bands (ev):

    99.0000  99.0000

 ------ SPIN DOWN ----------

          k = 0.0000 0.0000 0.0000 (   100 PWs)   bands (ev):

    88.0000  88.0000

     End of band structure calculation
     JOB DONE.
"""


def test_parse_bands_out_eigenvalues_uses_last_rerun():
    res = parse_bands_out_eigenvalues_from_text(STALE_PRIOR_RUN + NSPIN2_BANDS_OUT)
    assert res["nspin"] == 2
    # final run's values, NOT the stale 99/88 prior run
    assert res["eigs_up"] == [[16.0, 20.0], [17.5, 20.5], [15.0, 19.0]]
    assert res["eigs_dn"] == [[17.0, 18.004], [18.0, 19.0], [16.0, 18.006]]


def parse_bands_out_eigenvalues_from_text(text):
    """Helper: write text to a tmp file and parse (parser takes a path)."""
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".out")
    os.write(fd, text.encode())
    os.close(fd)
    try:
        return parse_bands_out_eigenvalues(Path(p))
    finally:
        os.unlink(p)


def _write_t2_inputs(tmp_path, bands_out_text):
    (tmp_path / "scf.out").write_text(SCF_OUT_MIN)
    (tmp_path / "bands_nscf.out").write_text(bands_out_text)
    (tmp_path / "bands.dat").write_text(BANDS_DAT_SPINUP)
    (tmp_path / "dos.dat").write_text(DOS_DAT_MIN)
    (tmp_path / "nscf_dos.out").write_text(NSCF_DOS_OUT_MIN)
    return dict(
        scf_out_path=tmp_path / "scf.out",
        bands_out_path=tmp_path / "bands_nscf.out",
        bands_dat_path=tmp_path / "bands.dat",
        dos_dat_path=tmp_path / "dos.dat",
        nscf_dos_out_path=tmp_path / "nscf_dos.out",
        initial_structure=NI_FCC_STRUCTURE,
        extraction_config={},
        is_metal_per_gt=True,
    )


def test_extract_t2_unions_spin_down_for_fm_metal(tmp_path):
    # spin-up alone → 1.5 eV gap; union with spin-down (crosses E_F) → metal.
    kw = _write_t2_inputs(tmp_path, NSPIN2_BANDS_OUT)
    res = extract_t2_fields(**kw)
    assert res["gap_type"] == "metal"
    assert res["band_gap_ev"] == 0.0
    # union fired → audit flag carried onto the metal short-circuit result
    assert res["diagnostics"] == {"nspin2_union": True}


def test_extract_t2_union_gated_off_for_gt_insulator(tmp_path):
    # GT insulator (is_metal_per_gt=False): union is out of scope (baked GT is
    # single-spin) → bands.dat untouched, no nspin2_union diagnostic, spin-up
    # gap preserved. Guards the deliberate metal-only scope.
    kw = _write_t2_inputs(tmp_path, NSPIN2_BANDS_OUT)
    kw["is_metal_per_gt"] = False
    res = extract_t2_fields(**kw)
    assert res["gap_type"] != "metal"
    assert "nspin2_union" not in (res["diagnostics"] or {})


def test_extract_t2_union_skipped_on_kcount_mismatch(tmp_path):
    # spin-down has fewer k-points than bands.dat → union skipped, no crash,
    # falls back to spin-up-only result (the pre-fix behavior).
    mismatched = NSPIN2_BANDS_OUT.replace(
        """
          k = 0.2000 0.0000 0.0000 (   100 PWs)   bands (ev):

    16.0000  18.0060
""",
        "",
    )
    kw = _write_t2_inputs(tmp_path, mismatched)
    res = extract_t2_fields(**kw)
    assert res["gap_type"] != "metal"
    assert res["band_gap_ev"] == pytest.approx(1.5, abs=1e-2)
    assert str(res["diagnostics"].get("nspin2_union", "")).startswith("skipped")

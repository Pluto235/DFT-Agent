"""Unit tests for harness.lib.evaluator.extract_t2_fields and helpers.

Covers:
- parse_alat_from_scf_out (celldm(1) → alat_angstrom)
- parse_fermi_energy_ev (SCF Fermi)
- parse_homo_lumo_ev (NSCF authoritative VBM/CBM 4dp)
- parse_bands_dat (QE bands.x output)
- read_dos_dat (QE dos.x output)
- match_k_to_seekpath_label (with periodicity + OFF_PATH)
- build_seekpath_input (initial_structure dict → seekpath cell tuple)
- extract_t2_fields (integration: synthetic Si T2 happy path)
- extract_t2_fields metal short-circuit (homo/lumo absent → metal)
- extract_t2_fields graceful failure (missing file → Nones)
- regression: homo/lumo override beats bands.dat rounding noise (MgO bug)
- regression: gap < 1e-3 → metal (Si false-metal bug)

No QE binary required. Uses small synthetic fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.lib.evaluator import (
    extract_t2_fields,
    match_k_to_seekpath_label,
    parse_alat_from_scf_out,
    parse_bands_dat,
    parse_fermi_energy_ev,
    parse_homo_lumo_ev,
    read_dos_dat,
)


# --------------------------------------------------------------------------
# parse_alat_from_scf_out
# --------------------------------------------------------------------------
def test_parse_alat_from_celldm_line(tmp_path):
    p = tmp_path / "scf.out"
    p.write_text(
        "     bravais-lattice index     =            2\n"
        "     lattice parameter (alat)  =      10.2864  a.u.\n"
        "     ...\n"
        "     celldm(1)=  10.286400  celldm(2)=   0.000000  celldm(3)=   0.000000\n"
    )
    alat_A = parse_alat_from_scf_out(p)
    # 10.2864 bohr × 0.5291772 = 5.4434 Å
    assert alat_A == pytest.approx(5.4434, abs=1e-3)


def test_parse_alat_prefers_celldm_over_lattice_param_line(tmp_path):
    """celldm(1) line has higher precision (6 dp); prefer it over the rounded
    'lattice parameter (alat)' line."""
    p = tmp_path / "scf.out"
    p.write_text(
        "     lattice parameter (alat)  =      10.2864  a.u.\n"
        "     celldm(1)=  10.286400  celldm(2)=   0.000000\n"
    )
    alat_A = parse_alat_from_scf_out(p)
    assert alat_A == pytest.approx(10.286400 * 0.5291772, abs=1e-6)


def test_parse_alat_raises_when_absent(tmp_path):
    p = tmp_path / "scf.out"
    p.write_text("Program PWSCF starts\nJOB DONE.\n")
    with pytest.raises(ValueError, match=r"alat|celldm"):
        parse_alat_from_scf_out(p)


# --------------------------------------------------------------------------
# parse_fermi_energy_ev / parse_homo_lumo_ev
# --------------------------------------------------------------------------
def test_parse_fermi_energy_metal(tmp_path):
    p = tmp_path / "scf.out"
    p.write_text("     the Fermi energy is    11.7421 ev\n")
    fe = parse_fermi_energy_ev(p)
    assert fe == pytest.approx(11.7421)


def test_parse_fermi_energy_insulator(tmp_path):
    p = tmp_path / "scf.out"
    p.write_text(
        "     highest occupied level (ev):     5.9840\n"
        "     (some text)\n"
        "     highest occupied level (ev):     6.1200\n"  # later one wins
    )
    fe = parse_fermi_energy_ev(p)
    assert fe == pytest.approx(6.1200)


def test_parse_homo_lumo_returns_tuple(tmp_path):
    p = tmp_path / "bands_nscf.out"
    p.write_text(
        "     highest occupied, lowest unoccupied level (ev):     6.1190    6.7100\n"
    )
    hl = parse_homo_lumo_ev(p)
    assert hl == (6.1190, 6.7100)


def test_parse_homo_lumo_returns_none_for_metal(tmp_path):
    p = tmp_path / "metal_nscf.out"
    p.write_text("     the Fermi energy is    11.7421 ev\n")
    assert parse_homo_lumo_ev(p) is None


def test_parse_homo_lumo_picks_last_occurrence(tmp_path):
    p = tmp_path / "ins.out"
    p.write_text(
        "     highest occupied, lowest unoccupied level (ev):     5.9844    6.6056\n"
        "     (intermediate text)\n"
        "     highest occupied, lowest unoccupied level (ev):     7.4676   11.9493\n"
    )
    assert parse_homo_lumo_ev(p) == (7.4676, 11.9493)


# --------------------------------------------------------------------------
# parse_bands_dat
# --------------------------------------------------------------------------
def _write_bands_dat(path: Path, nbnd: int, kpts_eigs: list) -> None:
    """Synthetic bands.dat: list of (k_xyz_2pi_alat, [eigs])."""
    nks = len(kpts_eigs)
    lines = [f" &plot nbnd={nbnd:5d}, nks={nks:5d} /"]
    for k, eigs in kpts_eigs:
        lines.append(f"  {k[0]:.6f}  {k[1]:.6f}  {k[2]:.6f}")
        # break into chunks of 10
        for i in range(0, len(eigs), 10):
            chunk = eigs[i:i + 10]
            lines.append("  " + "  ".join(f"{e:.4f}" for e in chunk))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_bands_dat_round_trip(tmp_path):
    p = tmp_path / "bands.dat"
    kpts_eigs = [
        ((0.0, 0.0, 0.0), [-5.5, -1.5, 5.5, 7.5]),
        ((0.025, 0.0, 0.025), [-5.4, -1.4, 5.6, 7.6]),
        ((0.05, 0.0, 0.05), [-5.3, -1.3, 5.7, 7.7]),
    ]
    _write_bands_dat(p, nbnd=4, kpts_eigs=kpts_eigs)
    out = parse_bands_dat(p)
    assert out["nbnd"] == 4
    assert out["nks"] == 3
    assert len(out["kpoints_cart_2pi_over_alat"]) == 3
    assert out["eigenvalues_ev"][0] == pytest.approx([-5.5, -1.5, 5.5, 7.5])
    assert out["eigenvalues_ev"][2][3] == pytest.approx(7.7)


# --------------------------------------------------------------------------
# read_dos_dat
# --------------------------------------------------------------------------
def test_read_dos_dat_with_efermi_header(tmp_path):
    p = tmp_path / "dos.dat"
    p.write_text(
        "#  E (eV)   dos(E)   Int dos(E)   EFermi =   6.1850 eV\n"
        "  -1.0000  0.0000E+00  0.0000E+00\n"
        "   0.0000  1.2300E-01  1.2300E-02\n"
        "   1.0000  2.5000E-01  2.5000E-01\n",
        encoding="utf-8",
    )
    out = read_dos_dat(p)
    assert len(out["energy_ev"]) == 3
    assert out["dos"][1] == pytest.approx(0.123)
    assert out["e_fermi_header"] == pytest.approx(6.1850)


# --------------------------------------------------------------------------
# match_k_to_seekpath_label
# --------------------------------------------------------------------------
def test_match_k_label_gamma_exact():
    pts = {"GAMMA": [0.0, 0.0, 0.0], "X": [0.5, 0.0, 0.5], "L": [0.5, 0.5, 0.5]}
    assert match_k_to_seekpath_label((0.0, 0.0, 0.0), pts) == "GAMMA"


def test_match_k_label_off_path():
    pts = {"GAMMA": [0.0, 0.0, 0.0], "X": [0.5, 0.0, 0.5], "L": [0.5, 0.5, 0.5]}
    # 0.42×X = (0.21, 0, 0.21): closest to GAMMA at distance ~0.30 > 0.05
    # → returns formatted coord string (杨博 0521: OFF_PATH sentinel → coord)
    assert match_k_to_seekpath_label((0.21, 0.0, 0.21), pts) == "(0.210, 0.000, 0.210)"


def test_match_k_label_periodicity():
    pts = {"GAMMA": [0.0, 0.0, 0.0]}
    assert match_k_to_seekpath_label((1.0, 0.0, 0.0), pts) == "GAMMA"
    assert match_k_to_seekpath_label((-0.01, 0.0, 0.0), pts) == "GAMMA"


def test_match_k_label_none_input_returns_off_path():
    pts = {"GAMMA": [0.0, 0.0, 0.0]}
    assert match_k_to_seekpath_label(None, pts) == "OFF_PATH"


# --------------------------------------------------------------------------
# extract_t2_fields integration: synthetic Si T2 happy path
# --------------------------------------------------------------------------
def _si_initial_structure() -> dict:
    """Si diamond primitive cell, FCC (cF), space group Fd-3m."""
    return {
        "lattice_constants_angstrom": {"a": 3.849, "b": 3.849, "c": 3.849},
        "lattice_angles_degree": {"alpha": 60.0, "beta": 60.0, "gamma": 60.0},
        "atomic_positions_crystal": [
            {"element": "Si", "position": [0.0, 0.0, 0.0]},
            {"element": "Si", "position": [0.25, 0.25, 0.25]},
        ],
    }


def test_build_seekpath_input_accepts_list_shape():
    """The evaluator accepts both list and mapping lattice representations."""
    import seekpath
    from harness.lib.evaluator import _build_seekpath_input

    si_list_shape = {
        "lattice_constants_angstrom": [3.849, 3.849, 3.849],
        "lattice_angles_degree": [60.0, 60.0, 60.0],
        "atomic_positions_crystal": [
            {"element": "Si", "position": [0.0, 0.0, 0.0]},
            {"element": "Si", "position": [0.25, 0.25, 0.25]},
        ],
    }
    cell_dict, pos_dict, num_dict = _build_seekpath_input(_si_initial_structure())
    cell_list, pos_list, num_list = _build_seekpath_input(si_list_shape)

    # 两种 shape 应产出相同 seekpath 输入（cell 矩阵 + frac 位置 + 元素序号）
    for r_d, r_l in zip(cell_dict, cell_list):
        for v_d, v_l in zip(r_d, r_l):
            assert abs(v_d - v_l) < 1e-9
    assert pos_dict == pos_list
    assert num_dict == num_list

    # 且 seekpath 实跑也成功，labels 一致
    sp_dict = seekpath.get_path((cell_dict, pos_dict, num_dict))
    sp_list = seekpath.get_path((cell_list, pos_list, num_list))
    assert set(sp_dict["point_coords"].keys()) == set(sp_list["point_coords"].keys())


def _write_si_scf_out(path: Path, alat_bohr: float = 10.2864, fermi_ev: float = 6.491):
    path.write_text(
        f"     lattice parameter (alat)  =      {alat_bohr:.4f}  a.u.\n"
        f"     celldm(1)=  {alat_bohr:.6f}  celldm(2)=   0.000000\n"
        "     End of self-consistent calculation\n"
        f"     the Fermi energy is    {fermi_ev:.4f} ev\n"
    )


def _write_si_bands_nscf_out(path: Path, homo: float = 6.1190, lumo: float = 6.7100):
    path.write_text(
        "     End of band structure calculation\n"
        f"     highest occupied, lowest unoccupied level (ev):     {homo:.4f}    {lumo:.4f}\n"
    )


def _write_si_dos_dat(path: Path, e_fermi: float = 6.1850):
    path.write_text(
        f"#  E (eV)   dos(E)   Int dos(E)   EFermi =   {e_fermi:.4f} eV\n"
        "   1.0000  0.0000E+00  0.0000E+00\n"
        "   2.0000  1.0000E-01  1.0000E-01\n"
        "   3.0000  2.0000E-01  3.0000E-01\n",
        encoding="utf-8",
    )


def _write_si_dos_nscf_out(path: Path, e_fermi: float = 6.1850):
    """Write a minimal DOS-NSCF pw.x output containing the Fermi energy line.

    After S3 (杨博 2026-05-17), this is the sole authoritative source for
    `dos_e_fermi` — dos.dat header and scf.out are NO LONGER fallbacks.
    """
    path.write_text(
        "     End of band structure calculation\n"
        f"     the Fermi energy is    {e_fermi:.4f} ev\n"
    )


def _write_si_synthetic_bands_dat(
    path: Path,
    *,
    alat_angstrom: float,
    homo: float = 6.1190,
    lumo: float = 6.7100,
    cbm_at_off_path: bool = True,
):
    """Synthesize a bands.dat where:
    - Path is GAMMA → X (Si FCC). X cartesian computed from seekpath's
      actual reciprocal_primitive_lattice on the given Si structure (so the
      synthetic stays internally consistent with extract_t2_fields' conversion).
    - 12 k-points along the segment, last one at X
    - 4 bands (1 deep + 1 VB + 2 CB)
    - VBM at GAMMA (idx 0), value = homo (4dp)
    - If cbm_at_off_path: CBM at idx 9 (~0.82×X) — should match OFF_PATH
      Else: CBM at idx 11 (=X) — should match X
    """
    import numpy as np
    import seekpath

    from harness.lib.evaluator import _build_seekpath_input
    sp = seekpath.get_path(_build_seekpath_input(_si_initial_structure()))
    B = np.array(sp["reciprocal_primitive_lattice"])  # rows in 1/Å (a · b.T = 2π·I)
    X_frac = np.array(sp["point_coords"]["X"])  # seekpath FCC X = (0.5, 0, 0.5)
    X_cart_per_A = X_frac @ B  # real cartesian in 1/Å
    # Convert real (1/Å) to bands.dat units (2π/alat): cart_per_A * alat / (2π)
    X_cart_2pi_alat = X_cart_per_A * alat_angstrom / (2.0 * np.pi)

    nks = 12
    nbnd = 4
    kpts_eigs = []
    for i in range(nks):
        t = i / (nks - 1)  # 0..1
        k_cart_2pi_alat = tuple(t * X_cart_2pi_alat)
        # Bands: deep core at -10, VB curving, CB curving
        # VBM at GAMMA (i=0): = homo. Decays as t increases.
        # CB minimum at i=9 (t≈0.82) if off-path, else at i=11 (t=1)
        cbm_idx = 9 if cbm_at_off_path else 11
        cb_min = lumo
        eigs = [
            -10.0,                                  # deep core
            homo - 0.5 * t,                         # VB top, max at t=0
            cb_min + 0.4 * abs(i - cbm_idx) / nks,  # CB bottom, min at cbm_idx
            cb_min + 1.5 + 0.3 * t,                 # higher CB
        ]
        kpts_eigs.append((k_cart_2pi_alat, eigs))
    _write_bands_dat(path, nbnd=nbnd, kpts_eigs=kpts_eigs)


def test_extract_t2_fields_si_happy_path(tmp_path):
    """End-to-end: synthetic Si T2 data → extract → verify all 6 fields."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100  # gap = 0.591
    _write_si_scf_out(scf, alat_bohr=alat_bohr)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    _write_si_dos_dat(dos_dat, e_fermi=6.1850)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=6.1850)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={"k_label_source": "seekpath"},
    )
    assert result["band_gap_ev"] == pytest.approx(0.591, abs=1e-3)
    assert result["gap_type"] == "indirect"
    assert result["vbm_k_label"] == "GAMMA"
    # CBM at idx 9 of 12 (t=9/11≈0.818) → frac = 0.818 × X = (0.409, 0, 0.409)
    # 杨博 0521: off-path returns coord string instead of "OFF_PATH" sentinel
    assert result["cbm_k_label"] == "(0.409, 0.000, 0.409)"
    assert result["dos_e_fermi"] == pytest.approx(6.1850, abs=1e-4)
    assert "energy_ev" in result["dos_curve"]
    assert "dos" in result["dos_curve"]
    assert len(result["dos_curve"]["energy_ev"]) == 3


def test_extract_t2_fields_metal_short_circuits(tmp_path):
    """When bands_nscf.out has no 'highest occupied, lowest unoccupied' line
    (= metal), extract_t2_fields returns gap=0 / gap_type='metal' /
    labels='metal'."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    _write_si_scf_out(scf, fermi_ev=11.7421)
    bands_nscf.write_text("     the Fermi energy is    11.7421 ev\n")  # metal
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=10.2864 * 0.5291772)
    _write_si_dos_dat(dos_dat)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=11.7421)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    assert result["band_gap_ev"] == 0.0
    assert result["gap_type"] == "metal"
    assert result["vbm_k_label"] == "metal"
    assert result["cbm_k_label"] == "metal"


def test_extract_t2_fields_missing_file_returns_nones(tmp_path):
    """If a required file is missing, extract returns Nones for affected fields
    (scoring layer judges as fail; doesn't crash harness)."""
    # All files missing — early return fires before dos_e_fermi extraction
    # (so the fail-fast on nscf_dos_out doesn't trigger).
    result = extract_t2_fields(
        scf_out_path=tmp_path / "missing_scf.out",
        bands_out_path=tmp_path / "missing_bands.out",
        bands_dat_path=tmp_path / "missing_bands.dat",
        dos_dat_path=tmp_path / "missing_dos.dat",
        nscf_dos_out_path=tmp_path / "missing_dos_nscf.out",
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    assert result["band_gap_ev"] is None
    assert result["gap_type"] is None
    assert result["vbm_k_label"] is None
    assert result["cbm_k_label"] is None
    assert result["dos_e_fermi"] is None
    assert result["dos_curve"] is None


def test_extract_t2_fields_homo_lumo_beats_bands_dat_rounding(tmp_path):
    """Regression: bands.dat is rounded to 1 meV; if SCF Fermi ~ rounded VB top
    (MgO bug), tight Fermi-splitter could misclassify VB as CB → spurious near-
    zero gap. Using homo_lumo_ev (4 dp) bypasses this, gives correct gap.

    Synthetic: VB top rounded to 7.468; NSCF reports HOMO=7.4676, LUMO=11.9493.
    Expected: gap = 11.9493 - 7.4676 = 4.4817 (4 dp values used directly).
    """
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    _write_si_scf_out(scf, fermi_ev=7.4676)
    _write_si_bands_nscf_out(bands_nscf, homo=7.4676, lumo=11.9493)
    # bands.dat with VB rounded to 7.468 (worse precision than NSCF homo line)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=10.2864 * 0.5291772,
                                  homo=7.468, lumo=11.949, cbm_at_off_path=False)
    _write_si_dos_dat(dos_dat, e_fermi=7.4700)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=7.4700)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    # Gap should be 11.9493 - 7.4676 = 4.4817 (from homo_lumo, not bands.dat)
    assert result["band_gap_ev"] == pytest.approx(4.4817, abs=1e-3)


def test_extract_t2_fields_fermi_fallback_when_smearing(tmp_path):
    """Regression: agent runs bands NSCF with `occupations='smearing'` (Run #2
    real case), so QE doesn't print 'highest occupied, lowest unoccupied' line.
    Must fall back to Fermi splitter using SCF Fermi energy, still get correct
    semiconductor result."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.119, 6.710
    # SCF Fermi from cold smearing sits between VBM and CBM
    _write_si_scf_out(scf, alat_bohr=alat_bohr, fermi_ev=6.491)
    # bands_nscf.out has NO homo/lumo line and NO Fermi line (smearing case
    # where neither propagates to nscf-bands output)
    bands_nscf.write_text(
        "     End of band structure calculation\n"
        "     number of k points=  242  Marzari-Vanderbilt smearing, width(Ry)= 0.01\n"
    )
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    _write_si_dos_dat(dos_dat, e_fermi=6.1850)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=6.1850)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    # Even via Fermi fallback, gap should be 0.591 (from bands.dat 4dp values),
    # gap_type indirect, VBM at GAMMA, CBM at frac (0.409, 0, 0.409) (~0.82×X).
    # 杨博 0521: off-path returns coord string instead of "OFF_PATH" sentinel
    assert result["band_gap_ev"] == pytest.approx(0.591, abs=2e-3)
    assert result["gap_type"] == "indirect"
    assert result["vbm_k_label"] == "GAMMA"
    assert result["cbm_k_label"] == "(0.409, 0.000, 0.409)"


def test_extract_t2_fields_cbm_at_x_assigns_x_label(tmp_path):
    """When CBM lands at the X point exactly (not 0.85×X), label should be 'X'
    not OFF_PATH. Sanity check that the seekpath label match works for non-Γ
    points."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_A = 10.2864 * 0.5291772
    homo, lumo = 4.0, 5.0  # gap=1.0, direct bandgap if VBM and CBM both at X
    _write_si_scf_out(scf)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=False)
    _write_si_dos_dat(dos_dat)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=6.1850)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    # CBM_idx=11 (last point) is at X. The synthetic builder also makes VB
    # decay smoothly so VBM is at idx=0 (GAMMA). Gap_type should be indirect
    # (GAMMA → X is not direct).
    assert result["cbm_k_label"] == "X"
    assert result["vbm_k_label"] == "GAMMA"
    assert result["gap_type"] == "indirect"


# --------------------------------------------------------------------------
# S3 (杨博 2026-05-17): dos_e_fermi 严格从 nscf_dos_out 抽，无 fallback
# --------------------------------------------------------------------------
def test_extract_t2_fermi_from_nscf_dos_out(tmp_path):
    """dos_e_fermi 必须来自 nscf_dos_out (DOS NSCF pw.x 输出) 的 Fermi 行。"""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100
    _write_si_scf_out(scf, alat_bohr=alat_bohr)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    # dos.dat header has a *different* fermi (6.0000); must be ignored.
    _write_si_dos_dat(dos_dat, e_fermi=6.0000)
    # nscf_dos.out is the sole authoritative source.
    nscf_dos.write_text("     the Fermi energy is     7.1234 ev\n")

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
    )
    assert result["dos_e_fermi"] == pytest.approx(7.1234, abs=1e-4)


def test_extract_t2_fermi_missing_raises(tmp_path):
    """空 nscf_dos.out（无 Fermi 行）→ evaluator 直接 ValueError，不静默回 None。"""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100
    _write_si_scf_out(scf, alat_bohr=alat_bohr)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    _write_si_dos_dat(dos_dat, e_fermi=6.1850)
    # Empty nscf_dos.out — no "the Fermi energy is" line.
    nscf_dos.write_text("     End of band structure calculation\n")

    with pytest.raises(ValueError, match=r"nscf_dos_out_path"):
        extract_t2_fields(
            scf_out_path=scf,
            bands_out_path=bands_nscf,
            bands_dat_path=bands_dat,
            dos_dat_path=dos_dat,
            nscf_dos_out_path=nscf_dos,
            initial_structure=_si_initial_structure(),
            extraction_config={},
        )


def test_extract_t2_fermi_no_fallback_to_dos_dat_header(tmp_path):
    """关键回归: dos.dat header 有 EFermi=5.555, nscf_dos.out 没 Fermi 行 →
    必须 ValueError（不能 fallback 到 5.555）。这条 lock 住 S3 移除 fallback 的行为。"""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100
    _write_si_scf_out(scf, alat_bohr=alat_bohr, fermi_ev=6.491)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    # dos.dat header HAS a fermi (5.555) — this MUST be ignored.
    _write_si_dos_dat(dos_dat, e_fermi=5.5550)
    # nscf_dos.out exists but has NO Fermi line.
    nscf_dos.write_text("     End of band structure calculation\n")

    with pytest.raises(ValueError, match=r"nscf_dos_out_path"):
        extract_t2_fields(
            scf_out_path=scf,
            bands_out_path=bands_nscf,
            bands_dat_path=bands_dat,
            dos_dat_path=dos_dat,
            nscf_dos_out_path=nscf_dos,
            initial_structure=_si_initial_structure(),
            extraction_config={},
        )


# --------------------------------------------------------------------------
# S6 (2026-05-17): legacy-detect support — dos_dat_already_relative kwarg
# --------------------------------------------------------------------------
def _write_dos_dat_with_energies(path: Path, energies: list[float],
                                 e_fermi_header: float = 6.1850):
    """Helper: write a dos.dat with arbitrary col-1 energies."""
    lines = [f"#  E (eV)   dos(E)   Int dos(E)   EFermi =   {e_fermi_header:.4f} eV"]
    for e in energies:
        lines.append(f"   {e:.4f}  1.0000E-01  1.0000E-01")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_extract_t2_dos_dat_already_relative_skip_subtract(tmp_path):
    """S6: when dos_dat_already_relative=True, dos_curve.energy_ev keeps
    col 1 verbatim (caller already knows it's EF-relative)."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100
    _write_si_scf_out(scf, alat_bohr=alat_bohr)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    # dos.dat col 1 already in [-5, +5] (EF-relative, agent mutated)
    energies_rel = [-5.0, -2.5, 0.0, 2.5, 5.0]
    _write_dos_dat_with_energies(dos_dat, energies_rel)
    # nscf_dos.out reports EF = 7.0; if we wrongly subtracted again range becomes [-12, -2]
    _write_si_dos_nscf_out(nscf_dos, e_fermi=7.0)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
        dos_dat_already_relative=True,
    )
    energies = result["dos_curve"]["energy_ev"]
    assert min(energies) == pytest.approx(-5.0, abs=1e-6)
    assert max(energies) == pytest.approx(5.0, abs=1e-6)


def test_extract_t2_dos_dat_already_relative_default_false(tmp_path):
    """S6: with default kwarg (False), extract subtracts EF as usual.
    Same input as the previous test but range collapses to [-12, -2]."""
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo, lumo = 6.1190, 6.7100
    _write_si_scf_out(scf, alat_bohr=alat_bohr)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    energies_rel = [-5.0, -2.5, 0.0, 2.5, 5.0]
    _write_dos_dat_with_energies(dos_dat, energies_rel)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=7.0)

    result = extract_t2_fields(
        scf_out_path=scf,
        bands_out_path=bands_nscf,
        bands_dat_path=bands_dat,
        dos_dat_path=dos_dat,
        nscf_dos_out_path=nscf_dos,
        initial_structure=_si_initial_structure(),
        extraction_config={},
        # default: dos_dat_already_relative=False
    )
    energies = result["dos_curve"]["energy_ev"]
    assert min(energies) == pytest.approx(-12.0, abs=1e-6)
    assert max(energies) == pytest.approx(-2.0, abs=1e-6)


# --------------------------------------------------------------------------
# Bonus: 真 Sn α Run 1 数据（仅 dev 机器存在，CI 跳过）
# --------------------------------------------------------------------------
def _safe_exists(p: Path) -> bool:
    """Path.exists() but treats a cross-user unreadable parent (PermissionError)
    as 'absent' — shared workspace: 这些 dev-only 路径属其它 user，shijie 读不到
    时 .exists() 会抛 PermissionError 而非返回 False，导致整个 test module 收集失败。"""
    try:
        return p.exists()
    except OSError:
        return False


_REAL_SN_DOS_NSCF = Path(os.getenv("BENCH_WEB_RUNS_DIR", "runs_local")) / (
    "2026-05-10T18-44-28Z_M1_T2_Sn_alpha_bands_dos/workdir/runs/sn_bands_dos/"
    "dos_nscf.out"
)


@pytest.mark.skipif(not _safe_exists(_REAL_SN_DOS_NSCF),
                    reason="real Sn α run dir not present on this machine")
def test_parse_fermi_real_sn_dos_nscf():
    assert parse_fermi_energy_ev(_REAL_SN_DOS_NSCF) == pytest.approx(7.412, abs=1e-3)


# --------------------------------------------------------------------------
# 议题 1 (2026-05-25 / v2 §1-Q1): metal short-circuit 阈值 1e-3 → 1e-2
# + is_metal_per_gt gate 防 GT=insulator 的 false-positive 短路
# --------------------------------------------------------------------------
def _build_small_gap_t2_fixture(tmp_path, gap_meV: float):
    """Build minimal T2 fixture set with homo/lumo line giving a gap of `gap_meV`
    meV. Returns dict of paths ready to pass to extract_t2_fields.

    Uses Si structure; homo=6.1190 eV (anchor), lumo = homo + gap.
    """
    scf = tmp_path / "scf.out"
    bands_nscf = tmp_path / "bands_nscf.out"
    bands_dat = tmp_path / "bands.dat"
    dos_dat = tmp_path / "dos.dat"
    nscf_dos = tmp_path / "dos_nscf.out"
    alat_bohr = 10.2864
    alat_A = alat_bohr * 0.5291772
    homo = 6.1190
    lumo = homo + gap_meV / 1000.0
    _write_si_scf_out(scf, alat_bohr=alat_bohr, fermi_ev=homo)
    _write_si_bands_nscf_out(bands_nscf, homo=homo, lumo=lumo)
    # bands.dat builder needs cbm > vbm; we point CBM off-path so the synthetic
    # builder doesn't produce a degenerate same-k VBM/CBM that confuses _find_vbm_cbm.
    _write_si_synthetic_bands_dat(bands_dat, alat_angstrom=alat_A,
                                  homo=homo, lumo=lumo, cbm_at_off_path=True)
    _write_si_dos_dat(dos_dat, e_fermi=homo)
    _write_si_dos_nscf_out(nscf_dos, e_fermi=homo)
    return dict(scf=scf, bands_nscf=bands_nscf, bands_dat=bands_dat,
                dos_dat=dos_dat, nscf_dos=nscf_dos)


def test_short_circuit_at_5meV_when_metal_gt(tmp_path):
    """议题 1 (v2 §1-Q1): GT=metal (is_metal_per_gt=True) + agent computes 5 meV gap
    → new threshold 1e-2 catches this (old 1e-3 missed it) → metal short-circuit."""
    f = _build_small_gap_t2_fixture(tmp_path, gap_meV=5.0)
    result = extract_t2_fields(
        scf_out_path=f["scf"], bands_out_path=f["bands_nscf"],
        bands_dat_path=f["bands_dat"], dos_dat_path=f["dos_dat"],
        nscf_dos_out_path=f["nscf_dos"],
        initial_structure=_si_initial_structure(),
        extraction_config={},
        is_metal_per_gt=True,
    )
    assert result["band_gap_ev"] == 0.0
    assert result["gap_type"] == "metal"
    assert result["vbm_k_label"] == "metal"
    assert result["cbm_k_label"] == "metal"


def test_no_short_circuit_when_gt_insulator(tmp_path):
    """议题 1 (v2 §1-Q1) **核心 gate**: GT=insulator (is_metal_per_gt=False) + agent
    computes 5 meV gap → 拒绝短路，保留 fermi-splitter 算出的真实小 gap。

    防止 GT=insulator 但 agent 算出微小 gap（k-mesh 稀 / smearing 没收敛）时
    被误判 metal → gap_type/vbm/cbm 三分类全错丢 0.35 weight 塌方。
    """
    f = _build_small_gap_t2_fixture(tmp_path, gap_meV=5.0)
    result = extract_t2_fields(
        scf_out_path=f["scf"], bands_out_path=f["bands_nscf"],
        bands_dat_path=f["bands_dat"], dos_dat_path=f["dos_dat"],
        nscf_dos_out_path=f["nscf_dos"],
        initial_structure=_si_initial_structure(),
        extraction_config={},
        is_metal_per_gt=False,
    )
    # 没短路：返回真实算出的 gap（≈ 0.005 eV）而非 0.0
    assert result["band_gap_ev"] == pytest.approx(0.005, abs=1e-4)
    # gap_type/labels 走 fermi-splitter 正常路径（不是 'metal'）
    assert result["gap_type"] != "metal"
    assert result["gap_type"] in ("direct", "indirect")
    assert result["vbm_k_label"] != "metal"
    assert result["cbm_k_label"] != "metal"


def test_legacy_none_gt_still_short_circuits(tmp_path):
    """议题 1 (v2 §1-Q1) legacy preservation: is_metal_per_gt=None (老 task /
    没传 GT.gap_type) 仍允许短路，不引入回归。"""
    f = _build_small_gap_t2_fixture(tmp_path, gap_meV=5.0)
    result = extract_t2_fields(
        scf_out_path=f["scf"], bands_out_path=f["bands_nscf"],
        bands_dat_path=f["bands_dat"], dos_dat_path=f["dos_dat"],
        nscf_dos_out_path=f["nscf_dos"],
        initial_structure=_si_initial_structure(),
        extraction_config={},
        is_metal_per_gt=None,
    )
    assert result["band_gap_ev"] == 0.0
    assert result["gap_type"] == "metal"


def test_threshold_bump_catches_8meV(tmp_path):
    """议题 1 (v2 §1-Q1) 阈值 bump 1e-3 → 1e-2 的具体收益：8 meV 之前 NOT 短路
    (因 8 > 1)，现在短路 (因 8 < 10)。救 Ge ≤10 meV 那批的核心 case。"""
    f = _build_small_gap_t2_fixture(tmp_path, gap_meV=8.0)
    result = extract_t2_fields(
        scf_out_path=f["scf"], bands_out_path=f["bands_nscf"],
        bands_dat_path=f["bands_dat"], dos_dat_path=f["dos_dat"],
        nscf_dos_out_path=f["nscf_dos"],
        initial_structure=_si_initial_structure(),
        extraction_config={},
        is_metal_per_gt=True,
    )
    assert result["band_gap_ev"] == 0.0
    assert result["gap_type"] == "metal"


def test_above_threshold_doesnt_short_circuit(tmp_path):
    """议题 1 (v2 §1-Q1) 已知残留: 12 meV > 10 meV → 不短路。
    handoff §B.3 acknowledge 的 Ge 23 meV / Rb 20-43 meV 漏网。"""
    f = _build_small_gap_t2_fixture(tmp_path, gap_meV=12.0)
    result = extract_t2_fields(
        scf_out_path=f["scf"], bands_out_path=f["bands_nscf"],
        bands_dat_path=f["bands_dat"], dos_dat_path=f["dos_dat"],
        nscf_dos_out_path=f["nscf_dos"],
        initial_structure=_si_initial_structure(),
        extraction_config={},
        is_metal_per_gt=True,
    )
    assert result["band_gap_ev"] == pytest.approx(0.012, abs=1e-4)
    assert result["gap_type"] in ("direct", "indirect")  # 不是 metal


# --------------------------------------------------------------------------
# _convert_kpts_cart_to_seekpath_frac round-trip guard (2π unit fix, 2026-05-25)
# --------------------------------------------------------------------------
def test_convert_kpts_cart_to_seekpath_frac_round_trip():
    """For each seekpath high-sym point, frac → cart (2π/alat) → back through
    the converter must round-trip to the original frac (< 1e-9 error).

    Guards against the 2π unit-conversion bug fixed 2026-05-25: previously
    `_convert_kpts_cart_to_seekpath_frac` did `k_cart / alat_A` then `@ inv(B)`
    where seekpath's reciprocal_primitive_lattice is in 1/Å (a · b.T = 2π·I),
    not 2π/Å as the docstring claimed; all extracted vbm/cbm_k_frac came out
    shrunk by ~2π toward Γ.
    """
    import numpy as np
    import seekpath
    from harness.lib.evaluator import (
        _build_seekpath_input,
        _convert_kpts_cart_to_seekpath_frac,
    )
    sp = seekpath.get_path(_build_seekpath_input(_si_initial_structure()))
    B = np.array(sp["reciprocal_primitive_lattice"])  # 1/Å
    alat_A = 10.2864 * 0.5291772  # Si alat
    inv_2pi_over_alat = alat_A / (2.0 * np.pi)
    for label, frac in sp["point_coords"].items():
        frac_arr = np.array(frac)
        cart_per_A = frac_arr @ B
        cart_2pi_alat = tuple(cart_per_A * inv_2pi_over_alat)
        recovered = _convert_kpts_cart_to_seekpath_frac(
            [cart_2pi_alat], alat_A, sp["reciprocal_primitive_lattice"],
        )[0]
        assert recovered == pytest.approx(frac_arr, abs=1e-9), (
            f"round-trip failed at {label}: in={frac} out={recovered}"
        )

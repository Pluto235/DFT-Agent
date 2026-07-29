"""Smoke tests for the T2 (bands + DOS) pipeline.

Covers:
- 8-point scan grid (5+3, key conventions, canonical key)
- t2_params.render_scf_input / render_nscf_input / render_bands_pw_input
  (including per-point ecutwfc/kmesh)
- t2_extract.parse_fermi_energy_ev (metal + insulator fixtures)
- t2_extract.find_vbm_cbm (synthetic eigenvalues)
- t2_extract.match_k_to_seekpath_label (GAMMA + OFF_PATH)
- t2_extract.parse_bands_dat (synthetic header+body)
- t2_extract.reconstruct_path_samples (segment indexing)
- t2_extract.extract_band_gap_from_bands_dat (semiconductor + metal)
- t2_analyze.classify_convergence (converged / marginal / not_converged / missing)
- canonical_gt_t2 JSON schema basic shape

No QE binary required. No network. No GPU.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.gt_selfrun.t2 import t2_analyze, t2_extract, t2_params

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


# --------------------------------------------------------------------------
# Geometry loader
# --------------------------------------------------------------------------
def test_load_t2_geometry_from_fixtures():
    geom = t2_params.load_t2_geometry(
        FIXTURES / "sample_si_t2.json",
        FIXTURES / "sample_si_t1canonical.json",
    )
    # Lattice parameters come from T1 canonical
    assert geom.a == pytest.approx(3.865)
    assert geom.alpha == pytest.approx(60.0)
    # Fractional positions come from T2 question
    assert len(geom.fractional_positions) == 2
    assert geom.species == ["Si", "Si"]
    assert geom.is_metal is False  # gap_type == "indirect"
    assert geom.pseudo_files == {"Si": "Si.upf"}


# --------------------------------------------------------------------------
# Input renderers
# --------------------------------------------------------------------------
def _make_si_geom() -> t2_params.T2Geometry:
    return t2_params.load_t2_geometry(
        FIXTURES / "sample_si_t2.json",
        FIXTURES / "sample_si_t1canonical.json",
    )


def test_render_scf_input_shape():
    geom = _make_si_geom()
    text = t2_params.render_scf_input(geom, FIXTURES, kmesh=[16, 16, 16])
    assert "calculation = 'scf'" in text
    assert "ecutwfc = 80.0" in text
    assert "ecutrho = 800.0" in text
    assert "K_POINTS (automatic)" in text
    assert "16 16 16 0 0 0" in text
    # non-metal -> fixed occupations
    assert "occupations = 'fixed'" in text
    assert "smearing" not in text
    assert "ATOMIC_SPECIES" in text
    assert "Si.upf" in text


def test_render_scf_input_metal_uses_smearing():
    g = _make_si_geom()
    metal_geom = t2_params.T2Geometry(
        a=g.a, b=g.b, c=g.c, alpha=g.alpha, beta=g.beta, gamma=g.gamma,
        species=["Cu"], fractional_positions=[[0.0, 0.0, 0.0]],
        pseudo_files={"Cu": "Cu.upf"}, is_metal=True,
    )
    text = t2_params.render_scf_input(metal_geom, FIXTURES, kmesh=[20, 20, 20])
    assert "smearing = 'mv'" in text
    assert "degauss = 0.02" in text
    assert "occupations = 'smearing'" in text


def test_render_nscf_input_shape():
    geom = _make_si_geom()
    text = t2_params.render_nscf_input(geom, FIXTURES, kmesh=[20, 20, 20], nbnd=24)
    assert "calculation = 'nscf'" in text
    assert "nbnd = 24" in text
    # Denser kmesh expected for NSCF (not validated by exact value, just present)
    assert "20 20 20 0 0 0" in text


def test_render_bands_pw_input_uses_crystal_b():
    geom = _make_si_geom()
    sp_path = [("GAMMA", "X"), ("X", "U"), ("K", "GAMMA"), ("GAMMA", "L")]
    sp_pts = {
        "GAMMA": [0.0, 0.0, 0.0],
        "X": [0.5, 0.0, 0.5],
        "U": [0.625, 0.25, 0.625],
        "K": [0.375, 0.375, 0.75],
        "L": [0.5, 0.5, 0.5],
    }
    text = t2_params.render_bands_pw_input(
        geom, FIXTURES, sp_path, sp_pts, nbnd=16, points_per_segment=20,
    )
    assert "calculation = 'bands'" in text
    assert "K_POINTS crystal_b" in text
    # First high-symmetry coord (GAMMA) should appear with n_per_seg=20 (not last)
    assert "0.0000000000 0.0000000000 0.0000000000 20" in text


def test_render_bands_pp_input():
    text = t2_params.render_bands_pp_input(filband="bands.dat")
    assert "&BANDS" in text
    assert "filband = 'bands.dat'" in text
    assert "lsym = .true." in text


def test_render_dos_input():
    text = t2_params.render_dos_input(fildos="dos.dat")
    assert "&DOS" in text
    assert "fildos = 'dos.dat'" in text
    assert "DeltaE = 0.01" in text
    assert "degauss = 0.02" in text


# --------------------------------------------------------------------------
# kmesh density helper — round to even, ≥2
# --------------------------------------------------------------------------
def test_kmesh_from_density_returns_even_ge2():
    geom = _make_si_geom()
    lat = t2_params.build_pmg_lattice(geom)
    km = t2_params.kmesh_from_density(lat, 50.0)
    assert len(km) == 3
    for n in km:
        assert n >= 2 and n % 2 == 0


def test_estimate_nbnd_si_minimum_8():
    geom = _make_si_geom()
    n = t2_params.estimate_nbnd(geom)
    # Si: 2 atoms × 4 valence = 8 electrons → 4 filled bands → +buffer ≥ 8 → nbnd ≥ 12
    assert n >= 12


# --------------------------------------------------------------------------
# Fermi-energy parser
# --------------------------------------------------------------------------
def test_parse_fermi_energy_metal():
    fe = t2_extract.parse_fermi_energy_ev(FIXTURES / "sample_scf_metal.out")
    assert fe is not None
    assert fe == pytest.approx(11.7421)


def test_parse_fermi_energy_insulator_uses_last_highest_occupied():
    fe = t2_extract.parse_fermi_energy_ev(FIXTURES / "sample_scf_insulator.out")
    assert fe is not None
    assert fe == pytest.approx(5.9840)  # last occurrence


# --------------------------------------------------------------------------
# VBM / CBM finder (synthetic data)
# --------------------------------------------------------------------------
def test_find_vbm_cbm_from_synthetic_indirect():
    # 3 k-points × 4 bands. Lower 3 bands filled (occ=1), top band empty.
    # VBM (top of band 3, max over k) at k1 = 5.99.
    # CBM (bottom of band 4, min over k) at k2 = 6.61.
    eigs = [
        [-5.0, -2.0, 5.50, 7.20],  # k0
        [-4.5, -1.5, 5.99, 7.50],  # k1  (VBM here)
        [-3.0,  0.0, 5.30, 6.61],  # k2  (CBM here)
    ]
    occs = [
        [1.0, 1.0, 1.0, 0.0],
        [1.0, 1.0, 1.0, 0.0],
        [1.0, 1.0, 1.0, 0.0],
    ]
    kpts = [(0.0, 0.0, 0.0), (0.5, 0.0, 0.5), (0.4, 0.4, 0.0)]
    res = t2_extract.find_vbm_cbm(eigs, occs, kpts, is_metal=False)
    assert res["vbm_ev"] == pytest.approx(5.99)
    assert res["cbm_ev"] == pytest.approx(6.61)
    assert res["vbm_k"] == (0.5, 0.0, 0.5)
    assert res["cbm_k"] == (0.4, 0.4, 0.0)
    assert res["band_gap_ev"] == pytest.approx(0.62)


def test_find_vbm_cbm_metal_returns_zero_gap():
    res = t2_extract.find_vbm_cbm([[1.0, 2.0]], [[1.0, 1.0]], [(0, 0, 0)], is_metal=True)
    assert res["band_gap_ev"] == 0.0
    assert res["is_metal_band"] is True


def test_find_vbm_cbm_uses_fermi_when_no_occupations():
    # No occupations provided → use fermi_ev as splitter.
    eigs = [
        [-2.0, -1.0, 1.0, 3.0],
        [-1.5, -0.5, 0.8, 2.7],
    ]
    res = t2_extract.find_vbm_cbm(
        eigs, occupations=None, kpoints_cryst=[(0, 0, 0), (0.3, 0, 0)],
        is_metal=False, fermi_ev=0.0,
    )
    # VBM = max of energies ≤ 0 → -0.5 at k1
    assert res["vbm_ev"] == pytest.approx(-0.5)
    # CBM = min of energies > 0 → 0.8 at k1
    assert res["cbm_ev"] == pytest.approx(0.8)


# --------------------------------------------------------------------------
# k-label matcher (B1)
# --------------------------------------------------------------------------
def test_match_k_to_seekpath_label_gamma():
    pts = {"GAMMA": [0.0, 0.0, 0.0], "X": [0.5, 0.0, 0.5], "L": [0.5, 0.5, 0.5]}
    assert t2_extract.match_k_to_seekpath_label((0.0, 0.0, 0.0), pts) == "GAMMA"
    # Slightly off but still under threshold
    assert t2_extract.match_k_to_seekpath_label((0.01, 0.0, 0.0), pts) == "GAMMA"


def test_match_k_to_seekpath_label_off_path():
    pts = {"GAMMA": [0.0, 0.0, 0.0], "X": [0.5, 0.0, 0.5], "L": [0.5, 0.5, 0.5]}
    # 0.42 down 1st coord is far from any (closest L → distance ≈ 0.71)
    assert t2_extract.match_k_to_seekpath_label((0.42, 0.0, 0.0), pts) == "OFF_PATH"
    # None input → OFF_PATH
    assert t2_extract.match_k_to_seekpath_label(None, pts) == "OFF_PATH"


def test_match_k_to_seekpath_handles_periodicity():
    pts = {"GAMMA": [0.0, 0.0, 0.0]}
    # k = (1.0, 0, 0) is GAMMA via periodicity
    assert t2_extract.match_k_to_seekpath_label((1.0, 0.0, 0.0), pts) == "GAMMA"
    # k = (-0.01, 0, 0) is GAMMA via wrap
    assert t2_extract.match_k_to_seekpath_label((-0.01, 0.0, 0.0), pts) == "GAMMA"


# --------------------------------------------------------------------------
# DOS file parser (handles a synthetic dos.dat)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Scan grid (8 点扫描)
# --------------------------------------------------------------------------
def test_scan_grid_has_8_points():
    grid = t2_params.scan_grid()
    assert len(grid) == 8
    keys = [p.key for p in grid]
    # 5 ecutwfc round 1 (kmesh density 30)
    assert "e30_d30" in keys and "e40_d30" in keys and "e80_d30" in keys
    # 3 kmesh round 2 (ecutwfc 80)
    assert "e80_d20" in keys and "e80_d40" in keys and "e80_d50" in keys
    # No overlap point e80_d30 in round 2 (it's round 1)
    round1 = [p for p in grid if p.round == 1]
    round2 = [p for p in grid if p.round == 2]
    assert len(round1) == 5 and len(round2) == 3


def test_canonical_scan_point():
    pt = t2_params.canonical_scan_point()
    assert pt.key == "e80_d50"
    assert pt.ecutwfc == 80.0
    assert pt.kmesh_density == 50.0
    assert pt.round == 2


def test_scan_grid_ecutrho_is_10x_ecutwfc():
    for p in t2_params.scan_grid():
        assert p.ecutrho == pytest.approx(p.ecutwfc * 10.0)


def test_render_scf_input_per_point():
    geom = _make_si_geom()
    text30 = t2_params.render_scf_input(
        geom, FIXTURES, kmesh=[8, 8, 8],
        ecutwfc=30.0, ecutrho=300.0,
    )
    assert "ecutwfc = 30.0" in text30
    assert "ecutrho = 300.0" in text30
    assert "8 8 8 0 0 0" in text30

    text80 = t2_params.render_scf_input(
        geom, FIXTURES, kmesh=[16, 16, 16],
        ecutwfc=80.0, ecutrho=800.0,
    )
    assert "ecutwfc = 80.0" in text80
    assert "ecutrho = 800.0" in text80
    assert "16 16 16 0 0 0" in text80


# --------------------------------------------------------------------------
# bands.dat parser & path-based VBM/CBM
# --------------------------------------------------------------------------
def _make_bands_dat(tmp_path, nbnd, kpts_eigs):
    """Helper: build a synthetic bands.dat. kpts_eigs = list of (k_xyz, eigs)."""
    nks = len(kpts_eigs)
    lines = [f" &plot nbnd={nbnd:5d}, nks={nks:5d} /"]
    for k, eigs in kpts_eigs:
        lines.append(f"  {k[0]:.6f}  {k[1]:.6f}  {k[2]:.6f}")
        # break eigs into lines of up to 10 floats
        chunks = [eigs[i:i + 10] for i in range(0, len(eigs), 10)]
        for ch in chunks:
            lines.append("  " + "  ".join(f"{e:.4f}" for e in ch))
    p = tmp_path / "bands.dat"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parse_bands_dat_round_trip(tmp_path):
    kpts_eigs = [
        ((0.0, 0.0, 0.0), [-5.5, -1.5, 5.5, 7.5]),
        ((0.025, 0.0, 0.025), [-5.4, -1.4, 5.6, 7.6]),
        ((0.05, 0.0, 0.05), [-5.3, -1.3, 5.7, 7.7]),
    ]
    path = _make_bands_dat(tmp_path, nbnd=4, kpts_eigs=kpts_eigs)
    out = t2_extract.parse_bands_dat(path)
    assert out["nbnd"] == 4
    assert out["nks"] == 3
    assert len(out["kpoints_cart_2pi_over_alat"]) == 3
    assert out["eigenvalues_ev"][0] == pytest.approx([-5.5, -1.5, 5.5, 7.5])
    assert out["eigenvalues_ev"][2][3] == pytest.approx(7.7)


def test_reconstruct_path_samples_count():
    flat_with_n = [
        ("GAMMA", [0.0, 0.0, 0.0], 5),
        ("X", [0.5, 0.0, 0.0], 5),
        ("L", [0.5, 0.5, 0.5], 1),
    ]
    samples = t2_extract.reconstruct_path_samples(flat_with_n)
    # 5 from GAMMA->X (incl GAMMA, excl X) + 5 from X->L (incl X, excl L) + 1 (L)
    assert len(samples) == 11
    # First sample is exactly at GAMMA
    assert samples[0]["k_cryst"] == [0.0, 0.0, 0.0]
    assert samples[0]["source_label"] == "vertex_GAMMA"
    # The 5th sample is at X (start of segment 2)
    assert samples[5]["k_cryst"] == [0.5, 0.0, 0.0]
    assert samples[5]["source_label"] == "vertex_X"
    # The last sample is at L
    assert samples[-1]["k_cryst"] == [0.5, 0.5, 0.5]
    assert samples[-1]["source_label"] == "vertex_L"


def test_extract_band_gap_from_bands_dat_semiconductor(tmp_path):
    # 11 samples along GAMMA-X-L, 4 bands. Make a clean indirect gap:
    #   - Lower 2 bands always around -5..-1 (occupied)
    #   - Upper 2 bands always around 0.5..2 (unoccupied, fermi=0)
    # VBM at GAMMA (idx 0)=–1.0; CBM at X (idx 5)=0.4
    flat_with_n = [
        ("GAMMA", [0.0, 0.0, 0.0], 5),
        ("X", [0.5, 0.0, 0.0], 5),
        ("L", [0.5, 0.5, 0.5], 1),
    ]
    kpts_eigs = []
    samples = t2_extract.reconstruct_path_samples(flat_with_n)
    for i, s in enumerate(samples):
        # encode the position via VBM/CBM dispersion to make GAMMA highest VBM
        # and X lowest CBM
        vbm_at_k = -1.0 - 0.5 * (i / len(samples)) ** 2  # max at i=0
        # CBM dips at i=5 (X)
        cbm_at_k = 0.4 + 0.6 * abs(i - 5) / 5  # min at i=5
        kpts_eigs.append(((s["k_cryst"][0], 0.0, 0.0),
                          [-5.0, vbm_at_k, cbm_at_k, 1.5]))

    bands_dat = _make_bands_dat(tmp_path, nbnd=4, kpts_eigs=kpts_eigs)
    res = t2_extract.extract_band_gap_from_bands_dat(
        bands_dat, flat_with_n,
        seekpath_point_coords={"GAMMA": [0.0, 0.0, 0.0],
                               "X": [0.5, 0.0, 0.0],
                               "L": [0.5, 0.5, 0.5]},
        fermi_ev=0.0, is_metal=False,
    )
    assert res["band_gap_ev"] == pytest.approx(1.4, abs=0.01)
    assert res["vbm_k_label"] == "GAMMA"
    assert res["cbm_k_label"] == "X"
    assert res["gap_type"] == "indirect"


def test_extract_band_gap_homo_lumo_override_beats_fermi_noise(tmp_path):
    """Regression test: bands.dat is rounded to 1 meV; if SCF Fermi happens to
    fall slightly *below* the rounded VB top (as observed for MgO at e40_d30),
    a tight eps would misclassify the VB as conduction and give a spurious
    near-zero gap. Passing homo_lumo_ev (parsed from NSCF) bypasses this.

    Synthetic case: VB top rounded to 7.468, NSCF reports HOMO=7.4676,
    LUMO=11.9493. With homo_lumo override → gap=4.4817; without (fermi-only)
    → also fixed by eps=0.005, so we test the override gives the higher-precision
    energy values.
    """
    samples = [
        ("GAMMA", [0.0, 0.0, 0.0], 1),
        ("X", [0.5, 0.0, 0.0], 1),
    ]
    # nbnd=4: 1 deep core, 1 shallow core, 1 VB, 1 CB. VB at Γ rounded to 7.468.
    # CBM is at Γ (lower than at X) so we expect cbm_k_label=GAMMA.
    kpts_eigs = [
        ((0.0, 0.0, 0.0),    [-65.0, -10.0, 7.468, 11.948]),
        ((0.25, 0.0, 0.0),   [-65.0, -10.0, 7.466, 12.500]),
    ]
    bands_dat = _make_bands_dat(tmp_path, nbnd=4, kpts_eigs=kpts_eigs)
    res = t2_extract.extract_band_gap_from_bands_dat(
        bands_dat, samples,
        seekpath_point_coords={"GAMMA": [0.0, 0.0, 0.0],
                               "X": [0.5, 0.0, 0.0]},
        fermi_ev=7.4676, is_metal=False,
        homo_lumo_ev=(7.4676, 11.9493),
    )
    assert res["band_gap_ev"] == pytest.approx(4.4817, abs=1e-3)
    assert res["vbm_ev"] == pytest.approx(7.4676, abs=1e-4)
    assert res["cbm_ev"] == pytest.approx(11.9493, abs=1e-4)
    assert res["vbm_k_label"] == "GAMMA"
    assert res["cbm_k_label"] == "GAMMA"
    assert res["gap_type"] == "direct"


def test_parse_homo_lumo_returns_none_for_metal_nscf(tmp_path):
    p = tmp_path / "metal_nscf.out"
    p.write_text("the Fermi energy is    15.8957 ev\n")
    assert t2_extract.parse_homo_lumo_ev(p) is None


def test_parse_homo_lumo_picks_last_occurrence(tmp_path):
    p = tmp_path / "ins_nscf.out"
    p.write_text(
        "highest occupied, lowest unoccupied level (ev):     5.9844    6.6056\n"
        "(some intermediate text)\n"
        "highest occupied, lowest unoccupied level (ev):     7.4676   11.9493\n"
    )
    assert t2_extract.parse_homo_lumo_ev(p) == (7.4676, 11.9493)


def test_extract_band_gap_metal_short_circuits(tmp_path):
    # is_metal=True → gap=0, label='metal' regardless of bands
    bands_dat = _make_bands_dat(tmp_path, nbnd=2,
                                kpts_eigs=[((0.0, 0.0, 0.0), [1.0, 2.0])])
    res = t2_extract.extract_band_gap_from_bands_dat(
        bands_dat, [("GAMMA", [0.0, 0.0, 0.0], 1)],
        seekpath_point_coords={"GAMMA": [0.0, 0.0, 0.0]},
        fermi_ev=1.5, is_metal=True,
    )
    assert res["band_gap_ev"] == 0.0
    assert res["gap_type"] == "metal"
    assert res["vbm_k_label"] == "metal"


# --------------------------------------------------------------------------
# Convergence classifier
# --------------------------------------------------------------------------
def _mk_point(key, round_, ecutwfc, density, gap, ef, status="OK"):
    return {
        "point_key": key,
        "round": round_,
        "ecutwfc_ry": ecutwfc,
        "kmesh_density": density,
        "status": status,
        "extracted": {"band_gap_ev": gap, "dos_e_fermi": ef},
    }


def _eight_points(r1_gaps, r2_gaps, r1_efs, r2_efs):
    """Build a synthetic 8-point summary list with given gap/Ef sequences.

    r1_gaps / r1_efs: 5-element sequences, ordered by ecutwfc (30,40,50,60,80)
    r2_gaps / r2_efs: 3-element sequences, ordered by density (20,40,50)
    """
    pts = []
    ecuts = [30, 40, 50, 60, 80]
    for ecut, g, ef in zip(ecuts, r1_gaps, r1_efs):
        pts.append(_mk_point(f"e{ecut}_d30", 1, ecut, 30, g, ef))
    densities = [20, 40, 50]
    for d, g, ef in zip(densities, r2_gaps, r2_efs):
        pts.append(_mk_point(f"e80_d{d}", 2, 80, d, g, ef))
    return pts


def test_classify_convergence_converged():
    # All last-two deltas < 0.02 (default thr) → converged
    pts = _eight_points(
        r1_gaps=[1.5, 1.4, 1.35, 1.31, 1.30],     # last two: |1.30-1.31|=0.01
        r2_gaps=[1.45, 1.32, 1.30],               # last two: |1.30-1.32|=0.02 → 边界
        r1_efs=[5.5, 5.55, 5.58, 5.60, 5.61],     # |5.61-5.60|=0.01
        r2_efs=[5.5, 5.60, 5.61],                 # |5.61-5.60|=0.01
    )
    res = t2_analyze.classify_convergence(pts)
    # r2 gap = 0.02 hits boundary; default thr=0.02 → not strictly < → marginal
    # Adjust expectation: all 4 deltas are 0.01 / 0.02 / 0.01 / 0.01
    # 0.02 is NOT < 0.02 → converged check fails. marginal_factor=2 → 0.04. 0.02 < 0.04 → marginal
    assert res["classification"] == "marginal"


def test_classify_convergence_clean_converged():
    # All deltas strictly below threshold
    pts = _eight_points(
        r1_gaps=[1.5, 1.4, 1.35, 1.31, 1.30],     # 0.01
        r2_gaps=[1.45, 1.305, 1.30],              # 0.005
        r1_efs=[5.5, 5.55, 5.58, 5.60, 5.61],     # 0.01
        r2_efs=[5.5, 5.605, 5.61],                # 0.005
    )
    res = t2_analyze.classify_convergence(pts)
    assert res["classification"] == "converged"


def test_classify_convergence_not_converged():
    # r1 gap big delta → blow past marginal (>= 0.04) → not_converged
    pts = _eight_points(
        r1_gaps=[2.0, 1.5, 1.0, 0.5, 0.4],        # 0.1 last two
        r2_gaps=[1.0, 0.41, 0.40],
        r1_efs=[5.5, 5.55, 5.58, 5.60, 5.61],
        r2_efs=[5.5, 5.60, 5.61],
    )
    res = t2_analyze.classify_convergence(pts)
    assert res["classification"] == "not_converged"


def test_classify_convergence_handles_missing():
    # If some point status != OK, the round may have <2 OK points → not_converged
    pts = _eight_points(
        r1_gaps=[1.0] * 5, r2_gaps=[1.0] * 3,
        r1_efs=[5.0] * 5, r2_efs=[5.0] * 3,
    )
    # Mark all of round 2 as failed
    for p in pts:
        if p["round"] == 2:
            p["status"] = "FAILED"
    res = t2_analyze.classify_convergence(pts)
    assert res["classification"] == "not_converged"
    assert "missing" in res["reason"].lower()


def test_classify_convergence_includes_evidence_block():
    pts = _eight_points(
        r1_gaps=[1.5, 1.4, 1.35, 1.31, 1.30],
        r2_gaps=[1.45, 1.32, 1.30],
        r1_efs=[5.5, 5.55, 5.58, 5.60, 5.61],
        r2_efs=[5.5, 5.60, 5.61],
    )
    res = t2_analyze.classify_convergence(pts)
    assert res["round1_ecutwfc"]["last_two_keys"] == ["e60_d30", "e80_d30"]
    assert res["round2_kmesh"]["last_two_keys"] == ["e80_d40", "e80_d50"]
    assert res["round1_ecutwfc"]["delta_band_gap_ev"] == pytest.approx(0.01)
    assert res["round2_kmesh"]["delta_band_gap_ev"] == pytest.approx(0.02)


# --------------------------------------------------------------------------
# canonical_gt_t2 JSON schema (built by t2_main.assemble_canonical_gt)
# --------------------------------------------------------------------------
def test_assemble_canonical_gt_shape():
    from tools.gt_selfrun.t2 import t2_main

    run_summary = {
        "material_id": "M1_T2_Si_bands_dos",
        "status": "OK",
        "is_metal": False,
        "wall_time_sec": 1234.5,
        "scan_points": [
            {"key": "e30_d30", "round": 1, "ecutwfc_ry": 30,
             "kmesh": [8, 8, 8], "kmesh_density": 30, "nbnd": 12,
             "step_walls_sec": {"scf": 10, "nscf": 10, "bands_pw": 10, "bands_pp": 1},
             "band_gap_ev": 0.6, "dos_e_fermi": 5.5,
             "vbm_k_label": "GAMMA", "cbm_k_label": "X", "gap_type": "indirect"},
        ] * 8,  # not really 8 distinct, but shape is what matters here
        "classification": {
            "classification": "converged",
            "round1_ecutwfc": {"delta_band_gap_ev": 0.01, "delta_e_fermi_ev": 0.01,
                              "last_two_keys": ["e60_d30", "e80_d30"], "scan_keys": []},
            "round2_kmesh": {"delta_band_gap_ev": 0.005, "delta_e_fermi_ev": 0.005,
                             "last_two_keys": ["e80_d40", "e80_d50"], "scan_keys": []},
            "thresholds": {"band_gap_thr_ev": 0.02, "e_fermi_thr_ev": 0.02,
                           "marginal_factor": 2.0},
            "reason": "all 4 deltas within converged thresholds",
        },
        "canonical": {
            "point_key": "e80_d50",
            "band_gap_ev": 0.62,
            "vbm_ev": 5.5, "cbm_ev": 6.12,
            "vbm_k_cryst": [0.0, 0.0, 0.0],
            "cbm_k_cryst": [0.5, 0.0, 0.0],
            "vbm_k_label": "GAMMA", "cbm_k_label": "X",
            "gap_type": "indirect",
            "dos_e_fermi": 5.5,
            "dos_dat_path": "/tmp/dos.dat",  # nonexistent — write_canonical handles
            "kmesh": [16, 16, 16],
        },
        "dos_params": {"sigma_ev": 0.02, "de_ev": 0.01},
    }
    canonical = t2_main.assemble_canonical_gt("M1_T2_Si_bands_dos", run_summary)
    assert canonical["id"] == "M1_T2_Si_bands_dos"
    assert canonical["schema_version"] == "1.2"
    gt = canonical["ground_truth"]
    assert set(gt.keys()) == {
        "band_gap_ev", "gap_type", "vbm_k_label", "cbm_k_label",
        "dos_e_fermi", "dos_curve",
    }
    assert gt["band_gap_ev"]["value"] == pytest.approx(0.62)
    assert gt["band_gap_ev"]["rule"] == "absolute"
    assert gt["gap_type"]["value"] == "indirect"
    assert gt["vbm_k_label"]["value"] == "GAMMA"
    assert gt["dos_curve"]["rule"] == "curve_l2"
    assert "Si" in gt["dos_curve"]["ref_file"]
    # Convergence evidence carries the 8-point + classification
    ev = canonical["convergence_evidence"]
    assert ev["classification"]["classification"] == "converged"
    assert ev["canonical_point_key"] == "e80_d50"
    # Raw audit data preserved
    assert canonical["extracted_raw"]["vbm_k_cryst"] == [0.0, 0.0, 0.0]


def test_assemble_canonical_gt_handles_failed_run():
    from tools.gt_selfrun.t2 import t2_main

    run_summary = {
        "material_id": "M1_T2_X_bands_dos",
        "status": "FAILED",
        "failed_at": "e30_d30/scf",
        "is_metal": False,
        "scan_points": [],
    }
    canonical = t2_main.assemble_canonical_gt("M1_T2_X_bands_dos", run_summary)
    assert canonical["status"] == "FAILED"
    assert canonical["failed_at"] == "e30_d30/scf"
    assert "ground_truth" not in canonical


def test_read_dos_dat_synthetic(tmp_path):
    dos_file = tmp_path / "dos.dat"
    dos_file.write_text(
        "#  E (eV)   dos(E)   Int dos(E)   EFermi =  5.6300 eV\n"
        "-1.0000  0.0000E+00  0.0000E+00\n"
        " 0.0000  1.2300E-01  1.2300E-02\n"
        " 1.0000  2.5000E-01  2.5000E-01\n",
        encoding="utf-8",
    )
    out = t2_extract.read_dos_dat(dos_file)
    assert len(out["energy_ev"]) == 3
    assert out["dos"][1] == pytest.approx(0.123)
    assert out["e_fermi_header"] == pytest.approx(5.63)

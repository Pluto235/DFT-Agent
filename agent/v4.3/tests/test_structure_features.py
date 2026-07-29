"""Structure feature extraction — bench prompt → {crystal_system, n_atoms, bucket}."""
from __future__ import annotations

from agent.structure_features import extract


# Sample header shape per bench corpus (M1_T2_*_bands_dos).
ALUMINUM_PROMPT = """\
For Al (fcc, space group Fm-3m, 1 atoms per primitive cell), run a full bands + DOS workflow.

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 2.856, b = 2.856, c = 2.856
  alpha = 60.0, beta = 60.0, gamma = 60.0

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  Al  0.000000  0.000000  0.000000

Pseudopotentials: GBRV_USPP_PBE_v1.5.
"""

SILICON_PROMPT = """\
For Si (diamond cubic, space group Fd-3m, 2 atoms per primitive cell), do a vc-relax.

Initial structure:
  a = 5.43, b = 5.43, c = 5.43
  alpha = 90.0, beta = 90.0, gamma = 90.0

ATOMIC_POSITIONS (crystal, fractional):
  Si  0.000  0.000  0.000
  Si  0.250  0.250  0.250

=== Submission Instructions ===
"vc_relax_out": path to vc-relax output
"""

# Tetragonal example — a=b≠c, all angles=90
TETRAGONAL_PROMPT = """\
For TiO2 (rutile, space group P4_2/mnm, 6 atoms per conventional cell), run SCF.

Initial structure:
  a = 4.594, b = 4.594, c = 2.959
  alpha = 90.0, beta = 90.0, gamma = 90.0

ATOMIC_POSITIONS (crystal):
  Ti  0.000  0.000  0.000
  Ti  0.500  0.500  0.500
  O   0.305  0.305  0.000
  O   0.695  0.695  0.000
  O   0.195  0.805  0.500
  O   0.805  0.195  0.500
"""

# Trigonal/rhombohedral example
RHOMBOHEDRAL_PROMPT = """\
For AlAs (rhombohedral primitive, 2 atoms per primitive cell), run SCF.

Initial structure (primitive cell):
  a = 4.013, b = 4.013, c = 4.013
  alpha = 60.0, beta = 60.0, gamma = 60.0

ATOMIC_POSITIONS (crystal):
  Al  0.000  0.000  0.000
  As  0.250  0.250  0.250
"""

# Hexagonal example
HEXAGONAL_PROMPT = """\
For ZnO (wurtzite, 4 atoms per primitive cell), run SCF.

Initial structure:
  a = 3.25, b = 3.25, c = 5.21
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  Zn  0.333  0.667  0.000
  Zn  0.667  0.333  0.500
  O   0.333  0.667  0.380
  O   0.667  0.333  0.880
"""

# Edge case: no structure info at all
EMPTY_PROMPT = "Just some prose, no lattice, no atoms.\n"


def test_extract_fcc_aluminum():
    f = extract(ALUMINUM_PROMPT)
    assert f["crystal_system"] == "cubic"   # Fm-3m → cubic (via pymatgen SG)
    assert f["n_atoms"] == 1
    assert f["n_atoms_bucket"] == "≤2"


def test_extract_diamond_silicon():
    f = extract(SILICON_PROMPT)
    assert f["crystal_system"] == "cubic"   # Fd-3m
    assert f["n_atoms"] == 2
    assert f["n_atoms_bucket"] == "≤2"


def test_extract_tetragonal_tio2():
    f = extract(TETRAGONAL_PROMPT)
    assert f["crystal_system"] == "tetragonal"
    assert f["n_atoms"] == 6
    assert f["n_atoms_bucket"] == "3-8"


def test_extract_rhombohedral_alas_via_lattice_fallback():
    """No 'space group' tag in header → falls back to lattice-parameter rules.
    a=b=c=4.013, α=β=γ=60° → trigonal (rhombohedral primitive)."""
    f = extract(RHOMBOHEDRAL_PROMPT)
    assert f["crystal_system"] == "trigonal"
    assert f["n_atoms"] == 2
    assert f["n_atoms_bucket"] == "≤2"


def test_extract_hexagonal_zno_via_lattice_fallback():
    f = extract(HEXAGONAL_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert f["n_atoms"] == 4
    assert f["n_atoms_bucket"] == "3-8"


def test_extract_empty_prompt_returns_unknown():
    f = extract(EMPTY_PROMPT)
    assert f["crystal_system"] == "unknown"
    assert f["n_atoms"] is None
    assert f["n_atoms_bucket"] == "unknown"


def test_extract_partial_header_only():
    """Header gives n_atoms via 'N atoms per primitive cell' even without
    ATOMIC_POSITIONS body. Verify the header regex works standalone."""
    prompt = "For Foo (cubic, space group Pm-3m, 5 atoms per primitive cell)."
    f = extract(prompt)
    assert f["crystal_system"] == "cubic"
    assert f["n_atoms"] == 5
    assert f["n_atoms_bucket"] == "3-8"


def test_extract_large_cell_bucket():
    """Many atoms in ATOMIC_POSITIONS block → >32 bucket via line counting."""
    body = "\n".join(f"  C  0.{i:03d}  0.000  0.000" for i in range(40))
    prompt = (
        "For C40 (some structure):\n"
        "  a = 10.0, b = 10.0, c = 10.0\n"
        "  alpha = 90.0, beta = 90.0, gamma = 90.0\n"
        "ATOMIC_POSITIONS (crystal):\n" + body + "\n"
    )
    f = extract(prompt)
    assert f["n_atoms"] == 40
    assert f["n_atoms_bucket"] == ">32"


def test_extract_lattice_fallback_for_unknown_space_group():
    """Even if space group symbol is junk, lattice rules still decide."""
    prompt = """\
For X (no useful header):
  a = 5.0, b = 5.0, c = 5.0
  alpha = 90.0, beta = 90.0, gamma = 90.0
"""
    f = extract(prompt)
    assert f["crystal_system"] == "cubic"


def test_extract_triclinic_lattice():
    prompt = """\
Mystery material:
  a = 5.0, b = 6.0, c = 7.0
  alpha = 70.0, beta = 80.0, gamma = 85.0
"""
    f = extract(prompt)
    assert f["crystal_system"] == "triclinic"


def test_extract_monoclinic_lattice():
    prompt = """\
Mystery material:
  a = 5.0, b = 6.0, c = 7.0
  alpha = 90.0, beta = 100.0, gamma = 90.0
"""
    f = extract(prompt)
    assert f["crystal_system"] == "monoclinic"


def test_extract_orthorhombic_lattice():
    prompt = """\
Mystery material:
  a = 5.0, b = 6.0, c = 7.0
  alpha = 90.0, beta = 90.0, gamma = 90.0
"""
    f = extract(prompt)
    assert f["crystal_system"] == "orthorhombic"


# ─── New fields: formula / elements / space_group_number / material_families ──


def test_new_fields_present_on_simple_silicon():
    f = extract(SILICON_PROMPT)
    assert f["formula"] == "Si"
    assert f["elements"] == ["Si"]
    assert f["space_group_number"] == 227   # Fd-3m
    assert "simple_semiconductor" in f["material_families"]


def test_new_fields_present_on_aluminum():
    f = extract(ALUMINUM_PROMPT)
    assert f["formula"] == "Al"
    assert f["elements"] == ["Al"]
    # Al is not in any of our family rules (not alkali, not group-13+15 combo,
    # not oxide, etc.) — empty families list is the correct outcome.
    assert f["material_families"] == []


def test_new_fields_present_on_tio2():
    f = extract(TETRAGONAL_PROMPT)
    assert f["formula"] == "TiO2"
    assert f["elements"] == ["O", "Ti"]
    assert "oxide" in f["material_families"]
    assert "transition_metal_oxide" in f["material_families"]


def test_new_fields_empty_prompt():
    f = extract(EMPTY_PROMPT)
    assert f["formula"] is None
    assert f["elements"] == []
    assert f["space_group_number"] is None
    assert f["material_families"] == []


# ─── §6.3 outlier targets ─────────────────────────────────────────────────────


RB_BCC_PROMPT = """\
For Rb (bcc, space group Im-3m, 1 atoms per primitive cell), run a full bands + DOS workflow.

Initial structure:
  a = 5.585, b = 5.585, c = 5.585
  alpha = 109.47, beta = 109.47, gamma = 109.47

ATOMIC_POSITIONS (crystal):
  Rb  0.000  0.000  0.000
"""


GAN_WURTZITE_PROMPT = """\
For GaN (wurtzite, space group P6_3mc, 4 atoms per primitive cell), run SCF + bands.

Initial structure:
  a = 3.189, b = 3.189, c = 5.185
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  Ga  0.333  0.667  0.000
  Ga  0.667  0.333  0.500
  N   0.333  0.667  0.376
  N   0.667  0.333  0.876
"""


HFO2_MONOCLINIC_PROMPT = """\
For HfO2 (monoclinic baddeleyite, space group P2_1/c, 12 atoms per primitive cell), run vc-relax.

Initial structure:
  a = 5.117, b = 5.175, c = 5.291
  alpha = 90.0, beta = 99.22, gamma = 90.0

ATOMIC_POSITIONS (crystal):
  Hf  0.276  0.040  0.208
  Hf  0.724  0.960  0.792
  Hf  0.776  0.540  0.292
  Hf  0.224  0.460  0.708
  O   0.070  0.330  0.345
  O   0.930  0.670  0.655
  O   0.570  0.830  0.155
  O   0.430  0.170  0.845
  O   0.450  0.755  0.480
  O   0.550  0.245  0.520
  O   0.950  0.255  0.020
  O   0.050  0.745  0.980
"""


SPODUMENE_PROMPT = """\
For LiAlSi2O6 (spodumene, monoclinic, space group C2/c, 20 atoms per primitive cell), run vc-relax.

Initial structure:
  a = 9.461, b = 8.395, c = 5.218
  alpha = 90.0, beta = 110.16, gamma = 90.0

ATOMIC_POSITIONS (crystal):
  Li  0.000  0.273  0.250
  Li  0.000  0.727  0.750
  Al  0.000  0.905  0.250
  Al  0.000  0.095  0.750
  Si  0.293  0.094  0.260
  Si  0.707  0.906  0.740
  Si  0.293  0.594  0.760
  Si  0.707  0.406  0.240
  O   0.110  0.078  0.140
  O   0.890  0.922  0.860
  O   0.110  0.578  0.640
  O   0.890  0.422  0.360
  O   0.360  0.265  0.299
  O   0.640  0.735  0.701
  O   0.360  0.765  0.799
  O   0.640  0.235  0.201
  O   0.355  0.985  0.060
  O   0.645  0.015  0.940
  O   0.355  0.485  0.560
  O   0.645  0.515  0.440
"""


def test_outlier_rb_alkali_metal():
    f = extract(RB_BCC_PROMPT)
    assert f["formula"] == "Rb"
    assert f["elements"] == ["Rb"]
    assert "alkali_metal" in f["material_families"]


def test_outlier_gan_polar_iii_v():
    f = extract(GAN_WURTZITE_PROMPT)
    assert f["formula"] == "GaN"
    assert f["elements"] == ["Ga", "N"]
    assert "polar_III_V" in f["material_families"]
    # GaN is not an oxide / halide / silicate — these should NOT appear.
    assert "oxide" not in f["material_families"]


def test_outlier_hfo2_transition_metal_oxide_monoclinic():
    f = extract(HFO2_MONOCLINIC_PROMPT)
    assert f["formula"] == "HfO2"
    assert f["elements"] == ["Hf", "O"]
    assert f["crystal_system"] == "monoclinic"
    assert "oxide" in f["material_families"]
    assert "transition_metal_oxide" in f["material_families"]


def test_outlier_spodumene_silicate():
    f = extract(SPODUMENE_PROMPT)
    assert f["formula"] == "LiAlSi2O6"
    # Spodumene has Li, Al, Si, O — sorted by symbol.
    assert f["elements"] == ["Al", "Li", "O", "Si"]
    assert "silicate" in f["material_families"]
    assert "oxide" in f["material_families"]
    # Li alone doesn't make it "alkali_metal" — that label requires a pure
    # alkali structure. Spodumene has multiple non-alkali elements.
    assert "alkali_metal" not in f["material_families"]


# ─── layered_vdw heuristic ──────────────────────────────────────────────────
#
# Trigger: crystal_system ∈ {hexagonal, trigonal} AND c/a > 1.8 AND elements
# overlap _LAYERED_SP2_ABLE (B,C,N,P,S,Se,Te) or _LAYERED_TMDC_METAL
# (Mo,W,Re,Nb,Ta,V,Ti,Zr,Hf,Sn). Distinguishes BN_hex / graphite / MoS2
# (c/a ≈ 2.7–4) from hcp metal / wurtzite (c/a ≈ 1.6 — must NOT fire).


BN_HEX_PROMPT = """\
For BN (hexagonal layered, space group P6_3/mmc, 4 atoms per primitive cell), run a full bands + DOS workflow.

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 2.511395, b = 2.511395, c = 8.441950
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  B  0.333333  0.666667  0.250
  B  0.666667  0.333333  0.750
  N  0.333333  0.666667  0.750
  N  0.666667  0.333333  0.250
"""

GRAPHITE_PROMPT = """\
For C (graphite, space group P6_3/mmc, 4 atoms per primitive cell), run SCF.

Initial structure:
  a = 2.461, b = 2.461, c = 6.708
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  C  0.000  0.000  0.250
  C  0.000  0.000  0.750
  C  0.333  0.667  0.250
  C  0.667  0.333  0.750
"""

MOS2_2H_PROMPT = """\
For MoS2 (2H polytype, space group P6_3/mmc, 6 atoms per primitive cell), run a bands+DOS.

Initial structure:
  a = 3.160, b = 3.160, c = 12.294
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  Mo  0.333  0.667  0.250
  Mo  0.667  0.333  0.750
  S   0.333  0.667  0.621
  S   0.333  0.667  0.879
  S   0.667  0.333  0.379
  S   0.667  0.333  0.121
"""

HCP_MG_PROMPT = """\
For Mg (hcp, space group P6_3/mmc, 2 atoms per primitive cell), run SCF.

Initial structure:
  a = 3.210, b = 3.210, c = 5.211
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  Mg  0.333  0.667  0.250
  Mg  0.667  0.333  0.750
"""


def test_layered_vdw_bn_hex_triggers():
    """BN_hex: c/a = 3.36 → layered_vdw + polar_III_V (B/N → group 13/15)."""
    f = extract(BN_HEX_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert "layered_vdw" in f["material_families"]
    # polar_III_V still fires — multi-label is intentional, advisors stack.
    assert "polar_III_V" in f["material_families"]


def test_layered_vdw_graphite_triggers():
    """Graphite: c/a = 2.72 → layered_vdw; C alone is simple_semiconductor too."""
    f = extract(GRAPHITE_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert "layered_vdw" in f["material_families"]
    assert "simple_semiconductor" in f["material_families"]


def test_layered_vdw_mos2_triggers():
    """MoS2 2H: c/a = 3.89, Mo in TMDC list + S in sp2-able list."""
    f = extract(MOS2_2H_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert "layered_vdw" in f["material_families"]


def test_layered_vdw_gan_wurtzite_does_not_trigger():
    """Wurtzite GaN: c/a = 1.63 < 1.8 → NOT layered (polar III-V w/ strong
    in-plane + out-of-plane covalent bonding, not vdW)."""
    f = extract(GAN_WURTZITE_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert "layered_vdw" not in f["material_families"]
    assert "polar_III_V" in f["material_families"]


def test_layered_vdw_hcp_mg_does_not_trigger():
    """hcp Mg: c/a = 1.62 < 1.8; pure metal, no sp2/TMDC element overlap."""
    f = extract(HCP_MG_PROMPT)
    assert f["crystal_system"] == "hexagonal"
    assert "layered_vdw" not in f["material_families"]


def test_layered_vdw_cubic_does_not_trigger():
    """Diamond Si is cubic (not hex/trig) — heuristic must short-circuit."""
    f = extract(SILICON_PROMPT)
    assert f["crystal_system"] == "cubic"
    assert "layered_vdw" not in f["material_families"]


# ─── Family-classification edge cases (no skill-trigger collisions) ──────────


def test_nacl_is_halide_not_alkali_metal():
    """Pure-alkali label must NOT fire on alkali halides — they're halide salts."""
    prompt = """\
For NaCl (rocksalt, space group Fm-3m, 2 atoms per primitive cell), run SCF.

Initial structure:
  a = 5.64, b = 5.64, c = 5.64
  alpha = 90.0, beta = 90.0, gamma = 90.0

ATOMIC_POSITIONS (crystal):
  Na  0.000  0.000  0.000
  Cl  0.500  0.500  0.500
"""
    f = extract(prompt)
    assert "halide" in f["material_families"]
    assert "alkali_metal" not in f["material_families"]


def test_sio2_is_oxide_not_silicate():
    """SiO2 alone is just an oxide — silicate label requires another cation."""
    prompt = """\
For SiO2 (quartz, space group P3_121, 9 atoms per primitive cell), run SCF.

Initial structure:
  a = 4.92, b = 4.92, c = 5.41
  alpha = 90.0, beta = 90.0, gamma = 120.0

ATOMIC_POSITIONS (crystal):
  Si  0.470  0.000  0.000
  Si  0.000  0.470  0.667
  Si  0.530  0.530  0.333
  O   0.413  0.267  0.119
  O   0.733  0.146  0.785
  O   0.854  0.587  0.452
  O   0.267  0.413  0.548
  O   0.146  0.733  0.215
  O   0.587  0.854  0.881
"""
    f = extract(prompt)
    assert "oxide" in f["material_families"]
    assert "silicate" not in f["material_families"]

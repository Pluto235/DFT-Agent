"""Tests for composition-based magnetism classification."""
from agent.subagents._magnetism import classify_magnetism, MagneticProfile


def test_elemental_ferromagnet_is_magnetic():
    p = classify_magnetism(["Fe"])
    assert p.is_magnetic is True
    assert p.magnetic_elements == ["Fe"]
    # FIX-33: Fe/Co/Ni carry a larger nonzero kick to avoid the paramagnetic
    # basin during itinerant-FM relaxation (Ni_fcc moment-collapse fix).
    assert p.seed["Fe"] == 0.7


def test_oxide_with_open_shell_tm_is_magnetic():
    p = classify_magnetism(["Cu", "O"])   # CuO — missed by v4.2 allowlist
    assert p.is_magnetic is True
    assert p.magnetic_elements == ["Cu"]


def test_mnf2_is_magnetic_via_mn():
    p = classify_magnetism(["Mn", "F"])
    assert p.is_magnetic is True
    assert p.seed["Mn"] == 0.7


def test_itinerant_fm_metals_have_bigger_seeds():
    # FIX-33: Fe/Co/Ni get a larger initial kick (still far below the real
    # per-atom moments, so not GT-fitted) to avoid the paramagnetic basin.
    assert classify_magnetism(["Co"]).seed["Co"] == 0.7
    assert classify_magnetism(["Ni"]).seed["Ni"] == 0.6


def test_nonmagnetic_semiconductor_is_not_magnetic():
    assert classify_magnetism(["Si"]).is_magnetic is False
    assert classify_magnetism(["Ga", "As"]).is_magnetic is False
    assert classify_magnetism(["Na", "Cl"]).is_magnetic is False


def test_closed_shell_d10_not_magnetic():
    # Zn/Cd are d10 — no local moment; must stay nonmagnetic
    assert classify_magnetism(["Zn", "O"]).is_magnetic is False


from agent.subagents._magnetism import parse_initial_structure, InitialStructure

_PROMPT = """For NiO (rocksalt, space group Fm-3m), relax the cell.

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 4.17, b = 4.17, c = 4.17
  alpha = 90.0, beta = 90.0, gamma = 90.0

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  Ni  0.000000  0.000000  0.000000
  Ni  0.500000  0.500000  0.000000
  O   0.500000  0.000000  0.000000
  O   0.000000  0.500000  0.000000

Pseudopotentials: GBRV_USPP_PBE_v1.5 (scalar-relativistic).
UPF files available in the symlinked ./pseudos/ directory:
  Ni: ni_pbe_v1.5.uspp.F.UPF
  O: o_pbe_v1.5.uspp.F.UPF
"""

def test_parse_initial_structure():
    s = parse_initial_structure(_PROMPT)
    assert isinstance(s, InitialStructure)
    assert (s.a, s.b, s.c) == (4.17, 4.17, 4.17)
    assert (s.alpha, s.beta, s.gamma) == (90.0, 90.0, 90.0)
    assert [site.element for site in s.sites] == ["Ni", "Ni", "O", "O"]
    assert s.sites[1].x == 0.5 and s.sites[1].y == 0.5
    assert s.pseudo_files == {"Ni": "ni_pbe_v1.5.uspp.F.UPF", "O": "o_pbe_v1.5.uspp.F.UPF"}

def test_parse_initial_structure_returns_none_on_garbage():
    assert parse_initial_structure("no structure here") is None

def test_parse_initial_structure_handles_scientific_notation():
    prompt = """For Foo (bar).

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 4.17e+00, b = 4.17e+00, c = 4.17e+00
  alpha = 9.0e+01, beta = 9.0e+01, gamma = 9.0e+01

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  Ni  0.0e+00  0.0e+00  0.0e+00
  O   5.0e-01  5.0e-01  0.0e+00

Pseudopotentials: stub
  Ni: ni.UPF
  O: o.UPF
"""
    s = parse_initial_structure(prompt)
    assert s is not None
    assert (s.a, s.b, s.c) == (4.17, 4.17, 4.17)
    assert (s.alpha, s.beta, s.gamma) == (90.0, 90.0, 90.0)
    assert s.sites[1].x == 0.5


import re as _re
from agent.subagents._magnetism import build_probe_scf_input


def _nio():
    return parse_initial_structure(_PROMPT)


def test_fm_input_has_nspin2_and_positive_seeds():
    s = _nio()
    prof = MagneticProfile(True, ["Ni"], {"Ni": 0.5})
    text = build_probe_scf_input(struct=s, profile=prof, ordering="FM", prefix="probe_fm")
    assert "nspin = 2" in text
    assert "calculation = 'scf'" in text
    # FM: single Ni species, positive seed; O present, seed 0
    assert "Ni " in text and "Ni1" not in text
    assert _re.search(r"starting_magnetization\(\d+\)\s*=\s*0\.5", text)
    # every site row uses element label Ni (not split)
    assert text.count("\nNi ") == 2


def test_afm_input_splits_species_with_alternating_signs():
    s = _nio()
    prof = MagneticProfile(True, ["Ni"], {"Ni": 0.5})
    text = build_probe_scf_input(struct=s, profile=prof, ordering="AFM", prefix="probe_afm")
    assert "nspin = 2" in text
    # AFM: Ni split into Ni1 / Ni2 in ATOMIC_SPECIES, same UPF
    assert "Ni1" in text and "Ni2" in text
    assert text.count("ni_pbe_v1.5.uspp.F.UPF") == 2  # both labels share the UPF
    assert _re.search(r"starting_magnetization\(\d+\)\s*=\s*0\.5", text)    # Ni1 +
    assert _re.search(r"starting_magnetization\(\d+\)\s*=\s*-0\.5", text)   # Ni2 -
    # the two Ni sites are assigned to Ni1 and Ni2 respectively
    assert "\nNi1 " in text and "\nNi2 " in text


_SPLIT_LABEL_PROMPT = """For Cr2O3 (corundum, R-3c), run bands + DOS.

Initial structure (primitive cell, lattice constants in angstrom, angles in degree):
  a = 5.3514, b = 5.3514, c = 5.3514
  alpha = 55.747, beta = 55.747, gamma = 55.747

ATOMIC_POSITIONS (crystal, i.e. fractional coordinates of the primitive cell):
  Cr1  0.849856  0.849856  0.849857
  Cr2  0.650144  0.650143  0.650144
  Cr1  0.150143  0.150144  0.150144
  Cr2  0.349856  0.349856  0.349856
  O  0.051691  0.749999  0.448308
  O  0.749998  0.448310  0.051692

Pseudopotentials: GBRV_USPP_PBE_v1.5
UPF files available in the symlinked ./pseudos/ directory:
  Cr: Cr.upf
  O: O.upf
  Cr1: Cr.upf
  Cr2: Cr.upf
"""


def test_parses_digit_suffixed_afm_labels():
    # FIX-36: M3 T2 prompts pre-split AFM sublattices (Cr1/Cr2). The parser must
    # NOT drop those sites; it normalises the label to the base element so the
    # probe can build a valid input (regression: Cr2O3 probe MPI_ABORTed with
    # only the O species when Cr1/Cr2 lines failed to match).
    s = parse_initial_structure(_SPLIT_LABEL_PROMPT)
    assert s is not None
    assert len(s.sites) == 6                       # 4 Cr + 2 O, none dropped
    assert sorted({x.element for x in s.sites}) == ["Cr", "O"]
    assert s.pseudo_files.get("Cr") == "Cr.upf"    # Cr1/Cr2 UPF lines normalised
    # the probe builds a valid Cr species (not just O)
    prof = MagneticProfile(True, ["Cr"], {"Cr": 0.5})
    afm = build_probe_scf_input(struct=s, profile=prof, ordering="AFM", prefix="p")
    assert "Cr1" in afm and "Cr2" in afm
    assert _re.search(r"starting_magnetization\(\d+\)\s*=\s*-0\.5", afm)


def test_input_has_cell_parameters_and_kpoints():
    s = _nio()
    prof = MagneticProfile(True, ["Ni"], {"Ni": 0.5})
    text = build_probe_scf_input(struct=s, profile=prof, ordering="FM")
    assert "CELL_PARAMETERS (angstrom)" in text
    assert "K_POINTS (automatic)" in text
    assert "ibrav = 0" in text


from agent.subagents._magnetism import parse_total_energy, select_ground_state


def test_parse_total_energy_takes_last_converged():
    out = "  total energy = -100.0 Ry\n!    total energy =     -1502.01795320 Ry\nJOB DONE.\n"
    assert parse_total_energy(out) == -1502.0179532


def test_parse_total_energy_none_when_absent():
    assert parse_total_energy("no energy line") is None


def test_select_ground_state_fm_lower():
    # NiO real numbers: FM lower than AFM -> FM wins (matches GT)
    ordering, reason = select_ground_state(fm_E=-1502.0179532, afm_E=-1502.01004008)
    assert ordering == "FM" and reason == "energy_comparison"


def test_select_ground_state_afm_lower():
    ordering, reason = select_ground_state(fm_E=-896.31853377, afm_E=-896.32833961)
    assert ordering == "AFM" and reason == "energy_comparison"


def test_select_ground_state_handles_one_failure():
    assert select_ground_state(fm_E=-10.0, afm_E=None) == ("FM", "afm_failed")
    assert select_ground_state(fm_E=None, afm_E=-10.0) == ("AFM", "fm_failed")
    assert select_ground_state(fm_E=None, afm_E=None) == ("FM", "both_failed_default")


from agent.subagents._magnetism import MagneticGroundState, render_magnetic_block


def test_render_block_fm():
    gs = MagneticGroundState(ok=True, ordering="FM", nspin=2,
                             magnetic_elements=["Ni"],
                             atomic_species_block="Ni 58.693 ni.UPF\nO 15.999 o.UPF",
                             starting_magnetization={"Ni": 0.5},
                             fm_energy_ry=-1502.018, afm_energy_ry=-1502.010,
                             reason="energy_comparison")
    block = render_magnetic_block(gs)
    assert "MAGNETIC GROUND STATE (probe)" in block
    assert "nspin = 2" in block
    assert "FM" in block
    assert "ni.UPF" in block          # ready-to-paste ATOMIC_SPECIES
    assert "starting_magnetization" in block
    # FIX-43: the plain-PBE engineering line stays (D2); the redundant
    # "Do NOT apply Hubbard U" restatement and any bench attribution are gone.
    assert "no Hubbard U" in block
    assert "Do NOT apply Hubbard U" not in block
    assert "bench" not in block.lower()


def test_render_block_empty_when_not_ok():
    assert render_magnetic_block(MagneticGroundState(ok=False)) == ""


def test_render_block_uses_numeric_indices_for_starting_magnetization():
    # QE requires starting_magnetization(<int>) — NOT label form like (Ni1).
    # Indices must be 1-based and match the ATOMIC_SPECIES insertion order.
    gs = MagneticGroundState(
        ok=True, ordering="AFM", nspin=2, magnetic_elements=["Ni"],
        atomic_species_block="  Ni1 58.693 ni.UPF\n  Ni2 58.693 ni.UPF\n  O 15.999 o.UPF",
        # dict insertion order matches ATOMIC_SPECIES order
        starting_magnetization={"Ni1": 0.5, "Ni2": -0.5},
        fm_energy_ry=-100.0, afm_energy_ry=-101.0, reason="energy_comparison",
    )
    block = render_magnetic_block(gs)
    # Numeric indices present (the QE-valid form)
    assert "starting_magnetization(1) = 0.5" in block
    assert "starting_magnetization(2) = -0.5" in block
    # The label-form mistake must NOT be emitted
    assert "starting_magnetization(Ni1)" not in block
    assert "starting_magnetization(Ni2)" not in block
    # The label is preserved as an audit comment
    assert "! Ni1" in block and "! Ni2" in block


# ---------------------------------------------------------------------------
# FIX-42 (S-1): probe robustness — gauss/0.01 smearing + no silent-FM fallback
# ---------------------------------------------------------------------------

def test_probe_input_uses_gauss_smearing_by_default():
    """The probe SCF must use gauss/0.01 (magnetic-friendly), not the
    moment-washing mv/0.02. Cr/Cu itinerant magnets oscillate under mv/0.02 →
    the probe SCFs never converge in budget → silent FM fallback (Cr_bcc/CuO
    2026-06-01). Mirrors FIX-33's production fix, which never reached the probe."""
    s = _nio()
    prof = MagneticProfile(True, ["Ni"], {"Ni": 0.5})
    for ordering in ("FM", "AFM"):
        text = build_probe_scf_input(struct=s, profile=prof, ordering=ordering)
        assert "smearing = 'gauss'" in text, f"{ordering}: not gauss"
        assert "degauss = 0.01" in text, f"{ordering}: not 0.01"
        assert "smearing = 'mv'" not in text


def test_select_ground_state_both_failed_afm_indicated():
    """When both probe SCFs fail BUT the structure ships AFM split sublattices,
    default to AFM (the bench's split-label materials are AFM), not a silent FM
    that loses the magnetisation and triggers the evaluator seekpath crash."""
    from agent.subagents._magnetism import select_ground_state
    assert select_ground_state(fm_E=None, afm_E=None, afm_indicated=True) == (
        "AFM", "both_failed_afm_indicated")
    # not AFM-indicated → unchanged FM default
    assert select_ground_state(fm_E=None, afm_E=None, afm_indicated=False) == (
        "FM", "both_failed_default")
    # backward-compat: default arg is False
    assert select_ground_state(fm_E=None, afm_E=None) == ("FM", "both_failed_default")


def test_prompt_indicates_afm_detects_split_labels():
    from agent.subagents._magnetism import prompt_indicates_afm
    assert prompt_indicates_afm(_SPLIT_LABEL_PROMPT) is True   # Cr1/Cr2 sublattices
    plain = ("a = 2.87, b = 2.87, c = 2.87\nalpha = 90, beta = 90, gamma = 90\n"
             "ATOMIC_POSITIONS (crystal)\n  Fe 0 0 0\n  Fe 0.5 0.5 0.5\n")
    assert prompt_indicates_afm(plain) is False               # plain Fe (FM)


def test_render_block_emits_structural_fallback_handoff():
    """On probe failure with an AFM-indicated structure, still hand the LLM the
    AFM split + seeds (structural fallback) so it carries them downstream —
    instead of returning '' and letting the LLM run a plain-FM single species."""
    from agent.subagents._magnetism import MagneticGroundState, render_magnetic_block
    gs = MagneticGroundState(
        ok=False, ordering="AFM", nspin=2, magnetic_elements=["Cr"],
        atomic_species_block="Cr1 51.996 Cr.upf\nCr2 51.996 Cr.upf",
        starting_magnetization={"Cr1": 0.5, "Cr2": -0.5},
        reason="both_failed_afm_indicated", structural_fallback=True)
    block = render_magnetic_block(gs)
    assert "Cr1" in block and "Cr2" in block
    assert "starting_magnetization" in block
    assert "nspin = 2" in block
    # FIX-43: neutral "structure-indicated" framing; no bench-marker leak
    assert "the bench's marker" not in block
    assert "structure-indicated" in block
    # a bare failure (no structural fallback) still renders nothing
    assert render_magnetic_block(MagneticGroundState(ok=False)) == ""

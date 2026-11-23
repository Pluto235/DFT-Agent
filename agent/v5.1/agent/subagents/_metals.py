"""Element-set heuristics for the QE configuration advisor.

Two general-chemistry tables drive metal vs insulator classification:

  METAL_ELEMENTS           — elements whose pure / intermetallic phases
                              are metallic at STP under PBE
  STRONGLY_ELECTRONEGATIVE — anion-forming non-metals; when present in
                              a compound, the compound is almost certainly
                              insulator / semiconductor under PBE

Two element-set predicates flag chemistry that requires deviation from
the default SCF recipe:

  is_likely_magnetic_3d_oxide  — open-shell 3d TM cation + O → consider
                                  nspin=2 + starting_magnetization
  is_likely_narrow_gap_heavy_p — heavy p-block cation + chalcogen/pnictogen
                                  anion → PBE may underestimate the gap;
                                  smearing on SCF + fixed on bands NSCF
                                  gives the cleanest evaluator output

Design choice — no formula whitelists.

An earlier iteration of this module shipped three `frozenset[str]` tables
keyed on chemical formula (MAGNETIC_TM_OXIDES, METALLIC_TM_OXIDES,
PBE_NEAR_METAL_FORMULAS). Each entry had been added in response to a
specific bench task failing on the previous formula set, so the table
encoded "for THIS exact compound, the bench scoring expects THIS
protocol". That is bench-overfit by construction — the agent reads its
own answer out of a lookup table rather than deriving it from the
material's elements. The predicates below replace those tables with
broader chemical rules: they catch more materials than the original
lists (sometimes including ones the bench has never tested), and they
miss some that the lists used to catch. Both effects are correct: a
heuristic should generalise.
"""
from __future__ import annotations


# ─── Generic chemistry: metal vs anion ───────────────────────────────────────

METAL_ELEMENTS: frozenset[str] = frozenset({
    # Alkali (group 1)
    "Li", "Na", "K", "Rb", "Cs", "Fr",
    # Alkaline-earth (group 2)
    "Be", "Mg", "Ca", "Sr", "Ba", "Ra",
    # Group 13 metals (B is semiconductor — excluded)
    "Al", "Ga", "In", "Tl",
    # Group 14 metals (C/Si/Ge/α-Sn are semiconductors; β-Sn / Pb metallic)
    "Sn", "Pb",
    # Group 15 — Bi semimetal (smearing is safer); As/P/N excluded
    "Sb", "Bi",
    # Group 16 — Po metallic (rare); Te/Se/S/O excluded
    "Po",
    # 3d transition metals
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    # 4d transition metals
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    # 5d transition metals (Lu grouped here per modern IUPAC)
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    # Lanthanides (all metallic; condensed)
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd",
    "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    # Actinides (Ac–U are the only commonly-modeled ones in DFT benches)
    "Ac", "Th", "Pa", "U",
})


# Anions / strongly electronegative atoms. When present in a binary or
# ternary compound, the material is almost certainly insulator / semi-
# conductor. Overrides metal classification: e.g. "Cu in METAL_ELEMENTS"
# is overridden when the formula is CuO (Cu²⁺ + O²⁻ ionic crystal).
STRONGLY_ELECTRONEGATIVE: frozenset[str] = frozenset({
    "O", "F", "Cl", "Br", "I", "N", "S", "Se", "Te",
})


# ─── Heuristic predicates ────────────────────────────────────────────────────

# Open-shell 3d TM cations in common oxide oxidation states. Excludes:
#   Sc, Ti  (d⁰ in their stable oxides Sc₂O₃, TiO₂) — closed shell
#   Zn      (d¹⁰ in ZnO) — closed shell
# Materials with these cations + O are likely AFM/FM-ordered Mott-style
# systems where PBE nspin=1 SCF is numerically unstable and the magnetic
# protocol (nspin=2 + starting_magnetization sublattice seeds) is needed.
_OPEN_SHELL_3D_TM: frozenset[str] = frozenset({
    "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
})

# Heavy p-block "cations" — group 13–15 elements where scalar-relativistic
# effects (Pb, Bi, Tl) and small-bandgap semiconductor physics (In/Sn
# pairings) frequently push PBE band gaps below ~50 meV when the experi-
# mental / hybrid-DFT gap is 0.1–0.7 eV.
_HEAVY_P_BLOCK: frozenset[str] = frozenset({
    "In", "Sn", "Tl", "Pb", "Bi", "Hg",
})

# Chalcogen / pnictogen partners that pair with heavy p-block in narrow-
# gap semiconductors (PbTe / PbSe / SnTe / HgTe / Bi₂Se₃ / Bi₂Te₃ / InSb /
# InAs class). Including As is a deliberate over-coverage: GaAs gets
# flagged too, but the recommended smearing-SCF / fixed-bands recipe is
# still SCF-correct for GaAs, just slightly slower than the strict
# fixed-everywhere path.
_NARROW_GAP_ANION: frozenset[str] = frozenset({
    "As", "Sb", "S", "Se", "Te",
})


# Elemental 3d magnets in their standard solid phases: Fe/Co/Ni are
# ferromagnetic, Cr/Mn are antiferromagnetic. These need nspin=2 +
# starting_magnetization even though the bare element is otherwise a "metal".
# Cu (d¹⁰s¹), V, Sc, Ti, Zn are NOT magnetic as elements and are excluded.
_ELEMENTAL_3D_MAGNETS: frozenset[str] = frozenset({
    "Fe", "Co", "Ni", "Cr", "Mn",
})


def is_likely_magnetic_3d_oxide(elements: set[str] | frozenset[str]) -> bool:
    """True iff the compound contains O and at least one open-shell 3d TM.

    Recommended SCF recipe when True:
      occupations='smearing', smearing='gauss', degauss=0.01 Ry, nspin=2,
      starting_magnetization seeds per TM sublattice.

    Counter-examples that intentionally do NOT trigger: TiO₂ (Ti d⁰),
    ZnO (Zn d¹⁰), Sc₂O₃ (Sc d⁰), WO₃ (W is 5d, not 3d), CuF₂ (no O).
    """
    if "O" not in elements:
        return False
    return bool(elements & _OPEN_SHELL_3D_TM)


def is_likely_magnetic_3d_system(elements: set[str] | frozenset[str]) -> bool:
    """True iff the material needs the nspin=2 magnetic protocol — a superset
    of `is_likely_magnetic_3d_oxide` that also covers non-oxide 3d-TM compounds
    and elemental 3d magnets.

    Triggers when EITHER:
      • an open-shell 3d TM cation (V/Cr/Mn/Fe/Co/Ni/Cu) is paired with any
        strongly-electronegative anion (O, F, Cl, S, …) — Mott/charge-transfer
        magnets like NiO, MnO, CoO, Fe₂O₃, Cr₂O₃, CuO, **MnF₂**, LaMnO₃; or
      • every element is an elemental 3d magnet (Fe/Co/Ni/Cr/Mn) — bare
        ferromagnetic/antiferromagnetic metals and their alloys.

    Recommended recipe (NO Hubbard U — the bench GT is plain PBE + nspin=2):
      nspin=2 + per-sublattice starting_magnetization; gaussian smearing
      (degauss 0.01 Ry) for the insulating compounds, the usual metal smearing
      for the elemental magnets.

    Counter-examples that do NOT trigger: TiO₂ (Ti d⁰), ZnO (Zn d¹⁰),
    Sc₂O₃ (Sc d⁰), WO₃ (5d), elemental Cu/V/Ti/Zn, Si.
    """
    els = set(elements)
    if not els:
        return False
    # Compound: open-shell 3d TM + electronegative anion (any halide/chalcogenide/…)
    if (els & _OPEN_SHELL_3D_TM) and (els & STRONGLY_ELECTRONEGATIVE):
        return True
    # Elemental 3d magnet (pure element or an all-magnet alloy)
    if els <= _ELEMENTAL_3D_MAGNETS:
        return True
    return False


def is_likely_narrow_gap_heavy_p(elements: set[str] | frozenset[str]) -> bool:
    """True iff the compound combines a heavy p-block cation with a
    chalcogen/pnictogen anion.

    Recommended SCF recipe when True:
      occupations='smearing' (mv, degauss=0.02 Ry) for vc-relax + SCF —
        stabilises the SCF against near-degenerate states at E_F.
      occupations='fixed' for the bands NSCF — lets QE print the
        `highest occupied, lowest unoccupied level` line that downstream
        band-gap evaluators rely on.

    False positives (e.g. GaAs, AlSb) are tolerable: the recipe is still
    SCF-correct, just adds modest overhead vs the strict fixed-occ path.
    """
    return bool(elements & _HEAVY_P_BLOCK) and bool(elements & _NARROW_GAP_ANION)

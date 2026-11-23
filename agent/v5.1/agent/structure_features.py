"""Extract structural features from a bench task prompt for experience-library scoping.

Bench prompts follow a consistent shape (verified against M1_T1 / M1_T2 corpus):

    For <FORMULA> (<structure_name>, space group <SG>, <N> atoms per primitive cell), ...
    ...
    Initial structure ...:
      a = ..., b = ..., c = ...
      alpha = ..., beta = ..., gamma = ...
    ATOMIC_POSITIONS (crystal, ...):
      <Symbol>  x  y  z
      ...

This module extracts:
  - crystal_system: cubic / tetragonal / orthorhombic / hexagonal / trigonal /
                    monoclinic / triclinic / unknown
  - n_atoms: int | None
  - n_atoms_bucket: "≤2" | "3-8" | "9-32" | ">32" | "unknown"
  - formula: str | None                  (header token; raw, not canonicalised)
  - elements: list[str]                  (sorted unique element symbols)
  - space_group_number: int | None       (via pymatgen SpaceGroup, when parseable)
  - material_families: list[str]         (multi-label family tags — see _families
                                          + layered_vdw heuristic in extract())

Never raises. On any parse failure, the corresponding field falls back to
"unknown" / None / [] and the function logs a one-line stderr note. The returned
dict is always shape-stable for downstream consumers.
"""
from __future__ import annotations

import math
import re
import sys
from typing import Optional


# Header pattern: "For X (...) ... <N> atoms per primitive cell"
_HEADER_RE = re.compile(
    r"For\s+[A-Za-z0-9()]+\s*\(([^)]+)\)",
)
_FORMULA_RE = re.compile(r"For\s+([A-Za-z][A-Za-z0-9_\-]*)\s*\(")
_SPACE_GROUP_RE = re.compile(r"space\s*group\s*([A-Za-z0-9_\-\/]+)", re.IGNORECASE)
_N_ATOMS_HEADER_RE = re.compile(
    r"(\d+)\s*atoms?\s*per\s*(primitive|conventional|unit)\s*cell",
    re.IGNORECASE,
)

# Lattice scalars
_A_RE = re.compile(r"\ba\s*=\s*([\d.eE+\-]+)")
_B_RE = re.compile(r"\bb\s*=\s*([\d.eE+\-]+)")
_C_RE = re.compile(r"\bc\s*=\s*([\d.eE+\-]+)")
_ALPHA_RE = re.compile(r"\balpha\s*=\s*([\d.eE+\-]+)", re.IGNORECASE)
_BETA_RE = re.compile(r"\bbeta\s*=\s*([\d.eE+\-]+)", re.IGNORECASE)
_GAMMA_RE = re.compile(r"\bgamma\s*=\s*([\d.eE+\-]+)", re.IGNORECASE)

# ATOMIC_POSITIONS block — count atoms by counting lines that begin with an
# element symbol followed by 3 floats. Lookahead is tight enough to skip the
# header line ("ATOMIC_POSITIONS (crystal, ...)").
_ATOM_LINE_RE = re.compile(
    r"^\s*([A-Z][a-z]?)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s*$",
    re.MULTILINE,
)


# ─── Family classification tables ────────────────────────────────────────────
# Element groups used to derive material_families. Single source of truth —
# skill triggers should reference family names rather than enumerating elements.

_ALKALI         = frozenset({"Li", "Na", "K", "Rb", "Cs", "Fr"})
_ALKALINE_EARTH = frozenset({"Be", "Mg", "Ca", "Sr", "Ba", "Ra"})
_GROUP_13       = frozenset({"B", "Al", "Ga", "In", "Tl"})
_GROUP_15       = frozenset({"N", "P", "As", "Sb", "Bi"})
_HALOGENS       = frozenset({"F", "Cl", "Br", "I", "At"})
_D_BLOCK        = frozenset({
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
})
# "Simple" elemental / group-IV covalent semiconductors. Diamond C lives here
# too because its electronic-structure recipe is closer to Si/Ge than to a
# transition-metal compound — fine to claim that label for the advisor library.
_SIMPLE_SEMI    = frozenset({"Si", "Ge", "C"})

# Layered vdW elements — paired with hex/trig + c/a > 1.8 in extract() to tag
# `layered_vdw`. NOT used in _families() because c/a needs lattice scalars.
_LAYERED_SP2_ABLE   = frozenset({"B", "C", "N", "P", "S", "Se", "Te"})
_LAYERED_TMDC_METAL = frozenset({"Mo", "W", "Re", "Nb", "Ta", "V", "Ti", "Zr", "Hf", "Sn"})


def _families(elements: set[str]) -> list[str]:
    """Map an element set to a list of material-family tags. Multi-label.

    Rules are intentionally conservative:
      - "alkali_metal" requires the structure to be *only* alkali metals
        (pure metal — NaCl is a halide, not an alkali metal).
      - "polar_III_V" requires elements ⊂ (group-13 ∪ group-15) with at least
        one of each (so GaN ✓ but Ga(NO3)3 ✗ because of O).
      - "silicate" requires Si+O *and* another cation (else it would catch SiO2,
        which the advisor library treats as a "simple_oxide" case via "oxide").
      - "transition_metal_oxide" requires a d-block element + O (HfO2 ✓, V2O5 ✓).
      - "oxide" is the weak label (anything containing O); intentionally
        overlapping with stronger labels so skills can target the right tier.
    """
    if not elements:
        return []
    fams: list[str] = []
    if elements <= _ALKALI:
        fams.append("alkali_metal")
    if elements <= _ALKALINE_EARTH:
        fams.append("alkaline_earth")
    if (elements & _GROUP_13) and (elements & _GROUP_15) \
            and elements <= (_GROUP_13 | _GROUP_15):
        fams.append("polar_III_V")
    if "O" in elements:
        fams.append("oxide")
    if elements & _HALOGENS:
        fams.append("halide")
    if {"Si", "O"} <= elements and (elements - {"Si", "O"}):
        fams.append("silicate")
    if (elements & _D_BLOCK) and "O" in elements:
        fams.append("transition_metal_oxide")
    if elements <= _SIMPLE_SEMI:
        fams.append("simple_semiconductor")
    return fams


# Crystal system derivation from (a, b, c, α, β, γ).
# Tolerance for "equal": 1% relative for lengths, 0.5° absolute for angles.
def _crystal_system_from_lattice(a: float, b: float, c: float,
                                 alpha: float, beta: float, gamma: float) -> str:
    def eq_len(x: float, y: float) -> bool:
        if max(x, y) <= 0:
            return False
        return abs(x - y) / max(x, y) < 0.01

    def eq_ang(x: float, y: float) -> bool:
        return abs(x - y) < 0.5

    ab = eq_len(a, b)
    bc = eq_len(b, c)
    ac = eq_len(a, c)

    al90 = eq_ang(alpha, 90.0)
    be90 = eq_ang(beta, 90.0)
    ga90 = eq_ang(gamma, 90.0)
    al_eq_be = eq_ang(alpha, beta)
    be_eq_ga = eq_ang(beta, gamma)
    al_eq_ga = eq_ang(alpha, gamma)
    ga120 = eq_ang(gamma, 120.0)

    # cubic: a=b=c and α=β=γ=90
    if ab and bc and ac and al90 and be90 and ga90:
        return "cubic"
    # trigonal (rhombohedral primitive): a=b=c and α=β=γ≠90
    if ab and bc and ac and al_eq_be and be_eq_ga and not al90:
        return "trigonal"
    # hexagonal: a=b, α=β=90, γ=120
    if ab and not ac and al90 and be90 and ga120:
        return "hexagonal"
    # tetragonal: a=b≠c and α=β=γ=90
    if ab and not ac and al90 and be90 and ga90:
        return "tetragonal"
    # orthorhombic: a,b,c all different and α=β=γ=90
    if not ab and not bc and not ac and al90 and be90 and ga90:
        return "orthorhombic"
    # monoclinic: one angle ≠ 90, other two = 90
    if (al90 + be90 + ga90) == 2:
        return "monoclinic"
    # triclinic: no other constraints met
    return "triclinic"


# Pymatgen SpaceGroup → (crystal_system, int_number) if symbol is parseable.
def _space_group_lookup(sg_symbol: str) -> tuple[Optional[str], Optional[int]]:
    try:
        from pymatgen.symmetry.groups import SpaceGroup  # type: ignore
        sg = SpaceGroup(sg_symbol)
        return sg.crystal_system, int(sg.int_number)
    except Exception:
        return None, None


def _bucket(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    if n <= 2:
        return "≤2"
    if n <= 8:
        return "3-8"
    if n <= 32:
        return "9-32"
    return ">32"


def _extract_elements_from_atomic_positions(prompt_text: str) -> list[str]:
    """Return sorted unique element symbols from the ATOMIC_POSITIONS block,
    bounded by the same body slice that n_atoms counting uses."""
    idx = prompt_text.find("ATOMIC_POSITIONS")
    if idx < 0:
        return []
    body = prompt_text[idx:]
    for stop in ("=== Submission", "=== Network", "Pseudopotentials:"):
        j = body.find(stop)
        if j > 0:
            body = body[:j]
            break
    syms: set[str] = set()
    for m in _ATOM_LINE_RE.finditer(body):
        syms.add(m.group(1))
    return sorted(syms)


def extract(prompt_text: str) -> dict:
    """Parse structural features from a bench task prompt.

    Never raises. Returns shape-stable dict; missing fields become
    "unknown" / None / [].
    """
    crystal_system = "unknown"
    n_atoms: Optional[int] = None
    formula: Optional[str] = None
    space_group_number: Optional[int] = None

    # 1. Formula (raw header token).
    fm = _FORMULA_RE.search(prompt_text)
    if fm:
        formula = fm.group(1)

    # 2. Header n_atoms.
    m = _N_ATOMS_HEADER_RE.search(prompt_text)
    if m:
        try:
            n_atoms = int(m.group(1))
        except ValueError:
            pass

    # 3. Header space group → crystal_system + int number (most reliable when present).
    sg_m = _SPACE_GROUP_RE.search(prompt_text)
    if sg_m:
        cs, sg_int = _space_group_lookup(sg_m.group(1).strip(",) "))
        if cs:
            crystal_system = cs
        if sg_int is not None:
            space_group_number = sg_int

    # 4. Fallback: lattice scalars → bravais rules.
    if crystal_system == "unknown":
        try:
            a = float(_A_RE.search(prompt_text).group(1))
            b = float(_B_RE.search(prompt_text).group(1))
            c = float(_C_RE.search(prompt_text).group(1))
            alpha = float(_ALPHA_RE.search(prompt_text).group(1))
            beta = float(_BETA_RE.search(prompt_text).group(1))
            gamma = float(_GAMMA_RE.search(prompt_text).group(1))
            if all(math.isfinite(x) and x > 0 for x in (a, b, c, alpha, beta, gamma)):
                crystal_system = _crystal_system_from_lattice(a, b, c, alpha, beta, gamma)
        except (AttributeError, ValueError, TypeError):
            pass

    # 5. Fallback n_atoms: count ATOMIC_POSITIONS body lines.
    if n_atoms is None:
        idx = prompt_text.find("ATOMIC_POSITIONS")
        if idx >= 0:
            body = prompt_text[idx:]
            for stop in ("=== Submission", "=== Network", "Pseudopotentials:"):
                j = body.find(stop)
                if j > 0:
                    body = body[:j]
                    break
            atoms = _ATOM_LINE_RE.findall(body)
            if atoms:
                n_atoms = len(atoms)

    # 6. Elements: from ATOMIC_POSITIONS block (most reliable source).
    elements = _extract_elements_from_atomic_positions(prompt_text)

    # 7. material_families derived from elements set.
    material_families = _families(set(elements))

    # 8. layered_vdw heuristic (hex/trig + c/a > 1.8 + sp2-able or TMDC element).
    # Distinguishes BN_hex / graphite / TMDC (c/a 2.7–4) from hcp metal / wurtzite
    # (c/a ≈ 1.6) so the advisor doesn't fire on Mg/Zn/Cd/GaN. PBE without vdW
    # underestimates layer spacing 3–5% — agent must NOT vc-relax c-axis freely.
    if crystal_system in ("hexagonal", "trigonal"):
        try:
            a = float(_A_RE.search(prompt_text).group(1))
            c = float(_C_RE.search(prompt_text).group(1))
            if a > 0 and (c / a) > 1.8:
                elem_set = set(elements)
                if (elem_set & _LAYERED_SP2_ABLE) or (elem_set & _LAYERED_TMDC_METAL):
                    material_families.append("layered_vdw")
        except (AttributeError, ValueError, TypeError):
            pass

    if crystal_system == "unknown" and n_atoms is None and not elements:
        print("[experience] structure parse failed: no SG, no lattice, no atom block",
              file=sys.stderr)

    return {
        "crystal_system": crystal_system,
        "n_atoms": n_atoms,
        "n_atoms_bucket": _bucket(n_atoms),
        "formula": formula,
        "elements": elements,
        "space_group_number": space_group_number,
        "material_families": material_families,
    }


def initial_structure_facts(prompt_text: str) -> dict:
    """Build the `initial` dict consumed by verify_vc_relax.

    The starting geometry (lattice + space group) is given to the agent in
    the task prompt, so this is task INPUT, not ground truth — using it to
    check that the relaxed cell preserves its symmetry is legitimate (it is
    NOT the graded answer, which is the relaxed result).

    Returns keys among {space_group_number, crystal_system, a, b, c,
    alpha, beta, gamma}. Any field that cannot be parsed is simply omitted,
    so the verifier transparently skips the corresponding check. Never raises.
    """
    facts: dict = {}
    try:
        feats = extract(prompt_text)
    except Exception:  # pragma: no cover — extract is itself never-raise
        feats = {}

    if feats.get("space_group_number") is not None:
        facts["space_group_number"] = feats["space_group_number"]
    cs = feats.get("crystal_system")
    if cs and cs != "unknown":
        facts["crystal_system"] = cs

    # Lattice scalars (optional): enable lattice-drift + angle-preservation.
    # Include the abc and angle triples independently — only when all three
    # of a triple parse to finite positive numbers.
    def _triple(keys, regexes):
        vals = {}
        for k, rx in zip(keys, regexes):
            try:
                vals[k] = float(rx.search(prompt_text).group(1))
            except (AttributeError, ValueError, TypeError):
                return {}
        if all(math.isfinite(v) and v > 0 for v in vals.values()):
            return vals
        return {}

    facts.update(_triple(("a", "b", "c"), (_A_RE, _B_RE, _C_RE)))
    facts.update(_triple(("alpha", "beta", "gamma"), (_ALPHA_RE, _BETA_RE, _GAMMA_RE)))
    return facts

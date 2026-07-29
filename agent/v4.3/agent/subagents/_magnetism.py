"""Composition-based magnetism logic for the M3 (magnetic-materials) path.

Pure functions only — no subprocess, no file I/O (those live in
magnetic_probe.py). This module decides WHICH materials are magnetic
(by chemistry, not by a task allowlist), builds the FM/AFM probe inputs,
parses energies, and selects the ground state mirroring the bench's
tools/gt_selfrun/m3/m3_run.py:_decide_magnetic_ground_state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Open-shell 3d transition metals + 4f lanthanides that carry a local
# moment in DFT. Chemistry-based, NOT a bench task list. Zn/Cd/Hg (d10),
# Sc (d0/d1 itinerant), and closed-shell ions are excluded — they do not
# sustain a local moment under collinear PBE.
MAGNETIC_ELEMENTS: frozenset[str] = frozenset({
    "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",   # 3d open-shell
    "Eu", "Gd",                                  # 4f local moments
})

# Initial |starting_magnetization| seed per element. Generic high-spin-ish
# guesses (deliberately well below the real per-atom moments, e.g. Fe~2.2 µB —
# NOT fitted to GT). For robust localized-moment oxides the converged moment is
# insensitive to the seed, BUT for itinerant elemental ferromagnets a too-small
# seed lets the SCF fall into the paramagnetic basin (FIX-33: Ni_fcc collapsed
# 7.85→0.00 µB under mv/0.02). So Fe/Co/Ni carry a larger nonzero kick.
MAGNETIC_SEED: dict[str, float] = {
    "V": 0.4, "Cr": 0.5, "Mn": 0.7, "Fe": 0.7,
    "Co": 0.7, "Ni": 0.6, "Cu": 0.4, "Eu": 0.7, "Gd": 0.7,
}
_DEFAULT_SEED = 0.5


@dataclass
class MagneticProfile:
    is_magnetic: bool
    magnetic_elements: list[str] = field(default_factory=list)
    seed: dict[str, float] = field(default_factory=dict)


def classify_magnetism(elements: list[str]) -> MagneticProfile:
    """Magnetic iff the composition contains any open-shell 3d/4f element."""
    mags = [e for e in elements if e in MAGNETIC_ELEMENTS]
    return MagneticProfile(
        is_magnetic=bool(mags),
        magnetic_elements=mags,
        seed={e: MAGNETIC_SEED.get(e, _DEFAULT_SEED) for e in mags},
    )


@dataclass
class Site:
    element: str
    x: float
    y: float
    z: float


@dataclass
class InitialStructure:
    a: float
    b: float
    c: float
    alpha: float
    beta: float
    gamma: float
    sites: list[Site] = field(default_factory=list)
    pseudo_files: dict[str, str] = field(default_factory=dict)


_ABC_RE = re.compile(r"a\s*=\s*([-+\d.eE]+),\s*b\s*=\s*([-+\d.eE]+),\s*c\s*=\s*([-+\d.eE]+)")
_ANG_RE = re.compile(r"alpha\s*=\s*([-+\d.eE]+),\s*beta\s*=\s*([-+\d.eE]+),\s*gamma\s*=\s*([-+\d.eE]+)")
# FIX-36 (2026-05-29): accept digit-suffixed species labels (Cr1, Co2, Mn1 …).
# M3 T2 prompts pre-split AFM sublattices into Cr1/Cr2 etc. The old
# `[A-Z][a-z]?` token stopped at the digit → the whole site/UPF line failed to
# match → the magnetic atoms were silently dropped, leaving a malformed probe
# input (only O species) that MPI_ABORTed (Cr2O3/CuO/MnO/MnF2/Fe2O3/Fe3O4/CoO
# T2 probes all failed → magnetism fell back to advisory-only). We normalise
# the label to its base element on parse so the existing _afm_labels splitting
# logic re-derives the sublattices from clean symbols.
_SITE_RE = re.compile(r"^\s*([A-Z][a-z]?)\d*\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s*$")
_UPF_RE = re.compile(r"^\s*([A-Z][a-z]?)\d*:\s*(\S+\.UPF)\s*$", re.IGNORECASE)


def base_element(label: str) -> str:
    """Strip a trailing AFM sublattice index from a species label: 'Cr1' -> 'Cr'.

    Returns the label unchanged if it is not a simple element(+digits) token.
    Shared by the probe input builder (ATOMIC_SPECIES) and the FIX-37 output
    normalizer in solve.py (ATOMIC_POSITIONS labels in the submitted vc-relax.out).
    """
    m = re.match(r"^([A-Z][a-z]?)\d*$", label)
    return m.group(1) if m else label


def parse_initial_structure(prompt_text: str) -> "InitialStructure | None":
    """Parse initial structure block from a prompt text.

    Extracts lattice constants (a, b, c, alpha, beta, gamma), atomic positions,
    and pseudopotential files from the formatted prompt text.

    Returns None if the structure is incomplete or malformed.
    """
    abc = _ABC_RE.search(prompt_text)
    ang = _ANG_RE.search(prompt_text)
    if not (abc and ang):
        return None
    a, b, c = (float(x) for x in abc.groups())
    alpha, beta, gamma = (float(x) for x in ang.groups())

    sites: list[Site] = []
    in_positions = False
    for line in prompt_text.splitlines():
        if "ATOMIC_POSITIONS" in line:
            in_positions = True
            continue
        if in_positions:
            m = _SITE_RE.match(line)
            if m:
                sites.append(Site(m.group(1), float(m.group(2)),
                                  float(m.group(3)), float(m.group(4))))
            elif sites:
                in_positions = False  # stop at first non-site line after the block

    pseudo_files: dict[str, str] = {}
    for line in prompt_text.splitlines():
        m = _UPF_RE.match(line)
        if m:
            pseudo_files[m.group(1)] = m.group(2)

    if not sites:
        return None
    return InitialStructure(a, b, c, alpha, beta, gamma, sites, pseudo_files)


def prompt_indicates_afm(prompt_text: str) -> bool:
    """True if the prompt's ATOMIC_POSITIONS block uses digit-suffixed
    sublattice labels (e.g. Cr1/Cr2) — the bench pre-splits magnetic sublattices
    this way for AFM materials. Used as the total-probe-failure fallback
    (FIX-42, S-1): an AFM-split structure defaults to AFM rather than a silent
    FM. parse_initial_structure normalises the labels to base elements, so the
    raw-token digit check has to be done here against the original text."""
    in_positions = False
    seen_site = False
    for line in prompt_text.splitlines():
        if "ATOMIC_POSITIONS" in line:
            in_positions = True
            continue
        if in_positions:
            if _SITE_RE.match(line):
                seen_site = True
                tok = line.split()[0]
                if re.match(r"^[A-Z][a-z]?\d+$", tok):
                    return True
            elif seen_site:
                break  # first non-site line after the block → done
    return False


import math

# Minimal atomic masses for ATOMIC_SPECIES (mass is irrelevant to an SCF
# energy — only used in MD — but QE requires a positive value).
_ATOMIC_MASS: dict[str, float] = {
    "H": 1.008, "Li": 6.94, "B": 10.81, "C": 12.011, "N": 14.007, "O": 15.999,
    "F": 18.998, "Na": 22.99, "Mg": 24.305, "Al": 26.982, "Si": 28.085,
    "P": 30.974, "S": 32.06, "Cl": 35.45, "K": 39.098, "Ca": 40.078,
    "Sc": 44.956, "Ti": 47.867, "V": 50.942, "Cr": 51.996, "Mn": 54.938,
    "Fe": 55.845, "Co": 58.933, "Ni": 58.693, "Cu": 63.546, "Zn": 65.38,
    "La": 138.905, "Eu": 151.964, "Gd": 157.25,
}
_DEFAULT_MASS = 50.0


def _cell_vectors_from_params(a, b, c, alpha, beta, gamma) -> list[tuple[float, float, float]]:
    """Crystallographic (a,b,c,α,β,γ in Å/deg) → 3 Cartesian cell vectors (Å).
    Standard convention: a along x, b in xy-plane."""
    ar, br, gr = (math.radians(x) for x in (alpha, beta, gamma))
    v1 = (a, 0.0, 0.0)
    v2 = (b * math.cos(gr), b * math.sin(gr), 0.0)
    cx = c * math.cos(br)
    cy = c * (math.cos(ar) - math.cos(br) * math.cos(gr)) / math.sin(gr)
    cz = math.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
    v3 = (cx, cy, cz)
    return [v1, v2, v3]


def _auto_kmesh(a: float, b: float, c: float, density: float = 30.0,
                kmin: int = 4) -> tuple[int, int, int]:
    return tuple(max(kmin, int(math.ceil(density / x))) for x in (a, b, c))  # type: ignore


def _afm_labels(sites: list[Site], magnetic_elements: list[str]) -> list[str]:
    """Per-site species label. Magnetic elements alternate E1/E2 over their
    own sites in input order; nonmagnetic elements keep their symbol."""
    counters: dict[str, int] = {e: 0 for e in magnetic_elements}
    labels: list[str] = []
    for s in sites:
        if s.element in counters:
            idx = counters[s.element]
            labels.append(f"{s.element}{1 if idx % 2 == 0 else 2}")
            counters[s.element] += 1
        else:
            labels.append(s.element)
    return labels


def build_probe_scf_input(*, struct: InitialStructure, profile: MagneticProfile,
                          ordering: str, ecutwfc: float = 50.0,
                          ecutrho_factor: float = 10.0, smearing: str = "gauss",
                          degauss: float = 0.01, prefix: str = "probe") -> str:
    """Render a single-point spin-polarized SCF pw.x input for ordering in
    {"FM","AFM"}. Modest cutoffs/k-mesh — this is for *energy ordering only*,
    not the final scored quantities.

    FIX-35 (2026-05-29): ecutwfc lowered 60→50 Ry to shrink the probe's
    wall-clock footprint on heavy AFM cells (Fe3O4/Fe2O3/LaMnO3 timeouts). The
    FM-vs-AFM total-energy *difference* is insensitive to cutoff (systematic
    error cancels); all observed probe margins (0.008–0.14 Ry) dwarf the
    sub-meV cutoff error on the difference, so ordering is unaffected.

    FIX-42 (S-1, 2026-06-01): smearing default mv/0.02 → gauss/0.01. The probe
    is magnetic-only; mv/0.02 (~3000 K) washes the d-band exchange splitting and
    itinerant magnets (Cr_bcc, CuO) oscillate → the probe SCFs never converge in
    budget → silent FM fallback (Cr_bcc/CuO 2026-06-01 both_failed_default). This
    mirrors FIX-33's production fix, which had never been propagated to the probe.

    Emits `pseudo_dir = '../pseudos'`, which assumes the magnetic_probe
    driver places this input one directory below the harness-provided
    `workdir/pseudos/` UPF symlink (i.e. the probe runs from
    `workdir/magnetic_probe/`). Any future harness change to the pseudos
    layout requires updating this path."""
    sites = struct.sites
    if ordering == "AFM":
        labels = _afm_labels(sites, profile.magnetic_elements)
    else:
        labels = [s.element for s in sites]

    # Unique species in first-appearance order.
    species: list[str] = []
    for lab in labels:
        if lab not in species:
            species.append(lab)

    # ATOMIC_SPECIES block (2-space indent is valid QE syntax and keeps species
    # lines distinct from ATOMIC_POSITIONS site rows when pattern-matching output)
    spec_lines = []
    for sp in species:
        el = base_element(sp)
        mass = _ATOMIC_MASS.get(el, _DEFAULT_MASS)
        upf = struct.pseudo_files.get(el, f"{el}.UPF")
        spec_lines.append(f"  {sp} {mass} {upf}")

    # starting_magnetization(i) per species index (1-based)
    mag_lines = []
    for i, sp in enumerate(species, 1):
        el = base_element(sp)
        if el in profile.seed:
            seed = profile.seed[el]
            if ordering == "AFM" and sp.endswith("2"):
                seed = -seed
            mag_lines.append(f"    starting_magnetization({i}) = {seed}")
        # nonmagnetic species: omit (QE defaults to 0)

    cell = _cell_vectors_from_params(struct.a, struct.b, struct.c,
                                     struct.alpha, struct.beta, struct.gamma)
    kx, ky, kz = _auto_kmesh(struct.a, struct.b, struct.c)
    nat = len(sites)
    ntyp = len(species)

    lines = [
        "&CONTROL",
        "    calculation = 'scf'",
        f"    prefix = '{prefix}'",
        "    outdir = './out'",
        "    pseudo_dir = '../pseudos'",
        "    tprnfor = .false.",
        "    tstress = .false.",
        "/",
        "&SYSTEM",
        "    ibrav = 0",
        f"    nat = {nat}",
        f"    ntyp = {ntyp}",
        f"    ecutwfc = {ecutwfc}",
        f"    ecutrho = {ecutwfc * ecutrho_factor}",
        "    occupations = 'smearing'",
        f"    smearing = '{smearing}'",
        f"    degauss = {degauss}",
        "    nspin = 2",
        *mag_lines,
        "/",
        "&ELECTRONS",
        "    conv_thr = 1.0e-6",
        "    mixing_beta = 0.3",
        "    electron_maxstep = 200",
        "/",
        "ATOMIC_SPECIES",
        *spec_lines,
        "CELL_PARAMETERS (angstrom)",
        *[f"  {v[0]:.10f}  {v[1]:.10f}  {v[2]:.10f}" for v in cell],
        "ATOMIC_POSITIONS (crystal)",
        *[f"{labels[i]} {s.x:.10f} {s.y:.10f} {s.z:.10f}" for i, s in enumerate(sites)],
        "K_POINTS (automatic)",
        f"{kx} {ky} {kz} 0 0 0",
        "",
    ]
    return "\n".join(lines)


_ETOT_RE = re.compile(r"^!\s+total energy\s*=\s*([-\d.]+)\s+Ry", re.MULTILINE)


def parse_total_energy(pw_out_text: str) -> "float | None":
    """Last converged '!    total energy = X Ry' value, or None.

    Quantum Espresso marks converged SCF energies with '!' prefix.
    Multi-iteration outputs (vc-relax, etc.) contain multiple matches;
    we return the final converged value.
    """
    ms = _ETOT_RE.findall(pw_out_text)
    return float(ms[-1]) if ms else None


def select_ground_state(*, fm_E: "float | None",
                        afm_E: "float | None",
                        afm_indicated: bool = False) -> tuple[str, str]:
    """Pick lower-energy ordering, mirroring bench
    tools/gt_selfrun/m3/m3_run.py:_decide_magnetic_ground_state. On a single
    failure use the survivor; on total failure fall back on the structure's
    AFM indication (see `afm_indicated`).

    Args:
        fm_E: Total energy of ferromagnetic configuration (Ry), or None if calculation failed.
        afm_E: Total energy of antiferromagnetic configuration (Ry), or None if calculation failed.
        afm_indicated: True when the task structure ships split magnetic
            sublattices (e.g. Cr1/Cr2) — the bench marks AFM materials this way.
            FIX-42 (S-1): on total probe failure, default to AFM rather than a
            silent FM. A silent FM on an AFM material loses the magnetisation
            (T1) and makes the LLM run a single-species cell that neither carries
            the moment (wrong e_fermi) nor lets FIX-40 fixed-occ recover the
            band fields (Cr_bcc/CuO 2026-06-01). FM is still the default when the
            structure is NOT split (genuine ferromagnets like bcc Fe).

    Returns:
        (ordering, reason) where ordering is "FM" or "AFM" and reason is one of:
        - "energy_comparison": both succeeded, lower energy wins
        - "fm_failed": AFM succeeded, FM failed
        - "afm_failed": FM succeeded, AFM failed
        - "both_failed_afm_indicated": both failed, structure is AFM-split → AFM
        - "both_failed_default": both failed, no AFM indication → FM
    """
    if fm_E is not None and afm_E is not None:
        return ("AFM", "energy_comparison") if afm_E < fm_E else ("FM", "energy_comparison")
    if fm_E is not None:
        return ("FM", "afm_failed")
    if afm_E is not None:
        return ("AFM", "fm_failed")
    if afm_indicated:
        return ("AFM", "both_failed_afm_indicated")
    return ("FM", "both_failed_default")


@dataclass
class MagneticGroundState:
    """Result of FM-vs-AFM probe + ground-state selection. Passed to Task 6
    (driver) and Task 9 (solve.py integration) to build context for the main
    solver run.

    Attributes:
        ok: True iff probe produced a usable ground state (at least one ordering succeeded).
        ordering: "FM" or "AFM" (the selected ground state).
        nspin: Spin-polarization degree (currently always 2 for collinear).
        magnetic_elements: List of open-shell 3d/4f element symbols in this material.
        atomic_species_block: Ready-to-paste ATOMIC_SPECIES rows with species labels
                              and UPF files. For AFM, includes split labels (E1/E2).
        starting_magnetization: Dict mapping species label (e.g. "Ni", "Ni1", "Ni2")
                                to signed seed value (Ry × 1e-2).
        fm_energy_ry: Total energy of FM configuration (Ry), or None if calculation failed.
        afm_energy_ry: Total energy of AFM configuration (Ry), or None if calculation failed.
        reason: One of "energy_comparison" | "fm_failed" | "afm_failed" | "both_failed_default".
        failure_reason: If ok=False, reason why the probe failed (string for logging).
    """
    ok: bool
    ordering: str = "FM"                 # "FM" | "AFM"
    nspin: int = 2
    magnetic_elements: list[str] = field(default_factory=list)
    atomic_species_block: str = ""       # ready-to-paste ATOMIC_SPECIES rows
    starting_magnetization: dict[str, float] = field(default_factory=dict)  # species label -> signed
    fm_energy_ry: "float | None" = None
    afm_energy_ry: "float | None" = None
    reason: str = ""
    failure_reason: "str | None" = None
    # FIX-42 (S-1): True when both probe SCFs failed but the structure is
    # AFM-split, so ordering was chosen by structure (AFM), not by energy. The
    # handoff is still rendered (with a caveat) so the LLM carries the AFM split
    # + seeds rather than running a silent single-species FM.
    structural_fallback: bool = False

    def to_json(self) -> str:
        """Serialize to JSON string for disk persistence (Task 9 wiring)."""
        import json
        from dataclasses import asdict
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def render_magnetic_block(gs: MagneticGroundState) -> str:
    """Text injected at the top of the main solver + vc_relax prompts.

    When MagneticGroundState.ok=False (probe failed entirely), returns empty string.
    Otherwise, formats a guidance block that summarizes the probe result, the selected
    ordering, and the exact ATOMIC_SPECIES + starting_magnetization to use in all
    downstream pw.x runs.

    Args:
        gs: MagneticGroundState from the completed probe.

    Returns:
        Multi-line markdown text (with ```-fenced code blocks) for context injection,
        or "" if gs.ok=False.
    """
    if not (gs.ok or gs.structural_fallback):
        return ""
    # Render as `starting_magnetization(i) = val   ! <label>` so the LLM gets
    # both the QE-required 1-based numeric index AND a label-to-index audit
    # trail. Insertion order of starting_magnetization MUST match the
    # ATOMIC_SPECIES order (the probe driver guarantees this).
    mag_lines = "\n".join(
        f"  starting_magnetization({i}) = {val}   ! {sp}"
        for i, (sp, val) in enumerate(gs.starting_magnetization.items(), 1)
    )
    if gs.structural_fallback:
        provenance = (
            f"This material is magnetic. The FM-vs-AFM probe SCFs did not "
            f"converge, but the structure ships split magnetic sublattices, so "
            f"**{gs.ordering}** is the structure-indicated ground state — use "
            f"it; do NOT collapse to a single-species ferromagnet."
        )
    else:
        provenance = (
            f"This material is magnetic. A deterministic FM-vs-AFM single-point "
            f"SCF probe selected **{gs.ordering}** as the ground state "
            f"(reason={gs.reason}; FM={gs.fm_energy_ry} Ry, AFM={gs.afm_energy_ry} Ry)."
        )
    return (
        f"## MAGNETIC GROUND STATE (probe) — FIX-32\n\n"
        f"{provenance} "
        f"Use plain PBE (no Hubbard U / lda_plus_u). nspin=2 with collinear spin.\n\n"
        f"USE in EVERY pw.x run (vc-relax + SCF + bands NSCF + DOS NSCF):\n"
        f"- `nspin = {gs.nspin}` in &SYSTEM (REQUIRED — do not drop it on any step).\n"
        f"- This exact ATOMIC_SPECIES block (note the AFM sublattice split if present;"
        f" the split labels share the same UPF):\n"
        f"```\nATOMIC_SPECIES\n{gs.atomic_species_block}\n```\n"
        f"- These starting_magnetization seeds (the `(i)` index matches the "
        f"ATOMIC_SPECIES order above 1-to-1; the `! <label>` comment is just "
        f"an audit trail — for AFM, the alternating signs define the sublattices):\n"
        f"```\n{mag_lines}\n```\n"
        f"Keep `mixing_beta = 0.3` and `electron_maxstep = 200` for SCF stability.\n"
    )

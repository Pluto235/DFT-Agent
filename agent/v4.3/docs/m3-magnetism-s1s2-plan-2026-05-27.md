# M3 Magnetism S1+S2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make v4.3 handle magnetic (M3) materials by (S1) classifying magnetism from composition and enforcing `nspin=2` + `starting_magnetization`, and (S2) determining the FM-vs-AFM ground state with a deterministic in-code dual-probe that mirrors the bench's own `_decide_magnetic_ground_state`.

**Architecture:** Two new modules under `agent/subagents/`: `_magnetism.py` (pure, testable: classification, structure parsing, QE-input building, energy parsing, ground-state selection, handoff rendering) and `magnetic_probe.py` (the I/O driver: builds FM + AFM single-point SCF inputs from the initial structure, runs `pw.x` twice, parses total energies, picks the lower one). The probe runs in `solve.py` Phase-1 *after* `config_advisor` and *before* `vc_relax`, only when the material is magnetic. Its result (winning ordering + ready-to-paste `ATOMIC_SPECIES` + `starting_magnetization`) is injected into the config-advice block consumed by `vc_relax` and the main loop, and `qe_input_lint` gains a backstop rule that auto-injects `nspin=2` if the LLM forgets.

**Tech Stack:** Python 3.10+, Quantum ESPRESSO `pw.x` (GPU podman wrapper or CPU), pytest. No new dependencies. Reuses `qe_tools.build_subprocess_env`, `detect_pw_x`, `run_loop`-style preflight pattern.

**Why this is not overfit (design constraint from the user):** Magnetism is keyed on *chemistry* (open-shell 3d/4f elements with local moments), not on the M3 task list. The ground-state decision *replicates the bench's methodology* (energy comparison of FM vs AFM) rather than hardcoding per-material orderings. The AFM candidate is the generic alternating-sign sublattice (same heuristic the bench GT uses). This generalizes to any magnetic material, including ones not in the current bench.

**Bench boundary:** Bench code is **read-only**. Issues found in bench are reported, not patched (see "Bench-side issues to report" at the end).

---

## Background (verified facts driving this plan)

- Batch `379913…baf1` (v4.2, deepseek-v4-pro, 30 M3 tasks) scored **0.409**. Root causes (see memory `m3-v42-failure-modes`):
  - v4.2 only forces `nspin=2` for a hardcoded 10-formula allowlist `MAGNETIC_TM_OXIDES` (`agent/subagents/_metals.py:110`). Misses Fe/Co/Ni/Cr elementals, CuO, Fe2O3, MnF2, CrO2, LaMnO3 → ran `nspin=1` → lattice + moment both 0 (Fe_bcc: confirmed no `nspin` in input).
  - Magnetic ordering is guessed from chemistry. GT picks ground state by FM-vs-AFM **energy comparison**. NiO: agent did textbook AFM, GT is PBE-**FM** (`fm_energy −1502.0180 Ry < afm −1502.0100`) → lost the 35%-weighted magnetization AND space group.
- GT protocol (memory `m3-bench-protocol`): **plain PBE + nspin=2 + starting_magnetization, NO Hubbard U.** `material.magnetic_metadata` (the answer) is **NOT** rendered into the solver prompt (`bench/harness/lib/task_loader.py:render_prompt` exposes only formula/structure/space_group/initial_structure/pseudopotential/advisory_notes). So the solver must infer magnetism and replicate the dual-probe itself.
- T1 weights: `total_magnetization_per_cell` 0.2 + `absolute_magnetization_per_cell` 0.15 = 35%. Tolerance 0.5 bohr/cell absolute. FM → total≈abs; AFM → total≈0, abs>0.
- `starting_magnetization` only seeds the *initial* SCF guess; the converged moment (what's scored) is insensitive to the exact seed magnitude, so generic seeds are fine.

---

## File Structure

- **Create** `agent/subagents/_magnetism.py` — pure functions/dataclasses (no subprocess, no file I/O). One responsibility: the magnetic *logic*.
- **Create** `agent/subagents/magnetic_probe.py` — the dual-probe driver (subprocess + workdir I/O). Mirrors `vc_relax.py` shape: `run()` → `_select()` → `render_magnetic_block()`.
- **Modify** `agent/subagents/config_advisor.py` — add magnetic fields to `ConfigAdvice`; call `classify_magnetism` in `_deterministic_classify`; carry fields through `_validate_payload`; (the rendered magnetic directive is replaced by the probe's handoff block downstream, so the legacy FIX-19 text stays but is superseded when a ground state is known).
- **Modify** `agent/tools/qe_input_lint.py` — add **Rule F**: when advice says magnetic, ensure `nspin=2` is present in pw.x inputs (auto-inject if missing); warn if no `starting_magnetization`.
- **Modify** `solve.py` — Phase-1 wiring: run `magnetic_probe` after `config_advisor`, before `vc_relax`, gated on `advice.is_magnetic`; inject the magnetic block; persist `.magnetic_ground_state.json`.
- **Create** `tests/test_magnetism.py`, `tests/test_magnetic_probe.py`; extend `tests/test_qe_input_lint*.py` (or the existing lint test module).

---

## Task 1: Composition-based magnetic classification

**Files:**
- Create: `agent/subagents/_magnetism.py`
- Test: `tests/test_magnetism.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_magnetism.py
from agent.subagents._magnetism import classify_magnetism, MagneticProfile

def test_elemental_ferromagnet_is_magnetic():
    p = classify_magnetism(["Fe"])
    assert p.is_magnetic is True
    assert p.magnetic_elements == ["Fe"]
    assert p.seed["Fe"] == 0.5

def test_oxide_with_open_shell_tm_is_magnetic():
    p = classify_magnetism(["Cu", "O"])   # CuO — missed by v4.2 allowlist
    assert p.is_magnetic is True
    assert p.magnetic_elements == ["Cu"]

def test_mnf2_is_magnetic_via_mn():
    p = classify_magnetism(["Mn", "F"])
    assert p.is_magnetic is True
    assert p.seed["Mn"] == 0.7

def test_nonmagnetic_semiconductor_is_not_magnetic():
    assert classify_magnetism(["Si"]).is_magnetic is False
    assert classify_magnetism(["Ga", "As"]).is_magnetic is False
    assert classify_magnetism(["Na", "Cl"]).is_magnetic is False

def test_closed_shell_d10_not_magnetic():
    # Zn/Cd are d10 — no local moment; must stay nonmagnetic
    assert classify_magnetism(["Zn", "O"]).is_magnetic is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetism.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.subagents._magnetism'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/_magnetism.py
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

# Initial |starting_magnetization| seed per element. Affects only the SCF
# initial guess; the converged (scored) moment is insensitive to it, so
# these are generic high-spin-ish guesses, not fitted to GT values.
MAGNETIC_SEED: dict[str, float] = {
    "V": 0.4, "Cr": 0.5, "Mn": 0.7, "Fe": 0.5,
    "Co": 0.5, "Ni": 0.5, "Cu": 0.4, "Eu": 0.7, "Gd": 0.7,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetism.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/_magnetism.py tests/test_magnetism.py
git commit -m "feat(v4.3): composition-based magnetic classification (S1, FIX-32)"
```

---

## Task 2: Parse the initial structure from the rendered prompt

**Files:**
- Modify: `agent/subagents/_magnetism.py`
- Test: `tests/test_magnetism.py`

The bench renders the structure as a fixed text block (`bench/harness/lib/task_loader.py:_render_initial_structure`): `  a = <>, b = <>, c = <>` / `  alpha = <>, beta = <>, gamma = <>`, then after a line containing `ATOMIC_POSITIONS (crystal`, rows `  <El>  <x>  <y>  <z>`. The pseudopotential block lists `  <El>: <file>.UPF`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_magnetism.py  (append)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetism.py::test_parse_initial_structure -q`
Expected: FAIL with `ImportError: cannot import name 'parse_initial_structure'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/_magnetism.py  (append)

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


_ABC_RE = re.compile(r"a\s*=\s*([-\d.eE]+),\s*b\s*=\s*([-\d.eE]+),\s*c\s*=\s*([-\d.eE]+)")
_ANG_RE = re.compile(r"alpha\s*=\s*([-\d.eE]+),\s*beta\s*=\s*([-\d.eE]+),\s*gamma\s*=\s*([-\d.eE]+)")
_SITE_RE = re.compile(r"^\s*([A-Z][a-z]?)\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s*$")
_UPF_RE = re.compile(r"^\s*([A-Z][a-z]?):\s*(\S+\.UPF)\s*$", re.IGNORECASE)


def parse_initial_structure(prompt_text: str) -> "InitialStructure | None":
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetism.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/_magnetism.py tests/test_magnetism.py
git commit -m "feat(v4.3): parse initial structure block for magnetic probe (S2)"
```

---

## Task 3: Build FM and AFM probe SCF inputs (incl. AFM species-split)

**Files:**
- Modify: `agent/subagents/_magnetism.py`
- Test: `tests/test_magnetism.py`

The AFM candidate is the generic alternating-sign sublattice: split each magnetic element `E` into species `E1`/`E2`, assign that element's sites alternately (1st→E1+, 2nd→E2−, 3rd→E1+, …), seed `starting_magnetization(E1)=+seed`, `(E2)=−seed`. FM keeps element labels, all magnetic species `+seed`. This is exactly the heuristic in `bench/tools/gt_selfrun/m3/m3_run.py` (`STARTING_MAG` + alternating site assignment).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_magnetism.py  (append)
from agent.subagents._magnetism import build_probe_scf_input, MagneticProfile, parse_initial_structure

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
    assert re.search(r"starting_magnetization\(\d+\)\s*=\s*0\.5", text)
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
    assert re.search(r"starting_magnetization\(\d+\)\s*=\s*0\.5", text)    # Ni1 +
    assert re.search(r"starting_magnetization\(\d+\)\s*=\s*-0\.5", text)   # Ni2 -
    # the two Ni sites are assigned to Ni1 and Ni2 respectively
    assert "\nNi1 " in text and "\nNi2 " in text

def test_input_has_cell_parameters_and_kpoints():
    s = _nio()
    prof = MagneticProfile(True, ["Ni"], {"Ni": 0.5})
    text = build_probe_scf_input(struct=s, profile=prof, ordering="FM")
    assert "CELL_PARAMETERS (angstrom)" in text
    assert "K_POINTS (automatic)" in text
    assert "ibrav = 0" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetism.py -k probe_scf -q` (and the three new tests)
Expected: FAIL with `ImportError: cannot import name 'build_probe_scf_input'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/_magnetism.py  (append)
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
                          ordering: str, ecutwfc: float = 60.0,
                          ecutrho_factor: float = 10.0, smearing: str = "mv",
                          degauss: float = 0.02, prefix: str = "probe") -> str:
    """Render a single-point spin-polarized SCF pw.x input for ordering in
    {"FM","AFM"}. Modest cutoffs/k-mesh — this is for *energy ordering only*,
    not the final scored quantities."""
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

    def base_element(label: str) -> str:
        m = re.match(r"^([A-Z][a-z]?)\d*$", label)
        return m.group(1) if m else label

    # ATOMIC_SPECIES block
    spec_lines = []
    for sp in species:
        el = base_element(sp)
        mass = _ATOMIC_MASS.get(el, _DEFAULT_MASS)
        upf = struct.pseudo_files.get(el, f"{el}.UPF")
        spec_lines.append(f"{sp} {mass} {upf}")

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetism.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/_magnetism.py tests/test_magnetism.py
git commit -m "feat(v4.3): build FM/AFM probe SCF inputs with AFM species-split (S2)"
```

---

## Task 4: Parse total energy + select ground state (mirror bench logic)

**Files:**
- Modify: `agent/subagents/_magnetism.py`
- Test: `tests/test_magnetism.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_magnetism.py  (append)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetism.py -k "energy or ground_state" -q`
Expected: FAIL with `ImportError: cannot import name 'parse_total_energy'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/_magnetism.py  (append)

_ETOT_RE = re.compile(r"^!\s+total energy\s*=\s*([-\d.]+)\s+Ry", re.MULTILINE)


def parse_total_energy(pw_out_text: str) -> "float | None":
    """Last converged '!    total energy = X Ry' value, or None."""
    ms = _ETOT_RE.findall(pw_out_text)
    return float(ms[-1]) if ms else None


def select_ground_state(*, fm_E: "float | None",
                        afm_E: "float | None") -> tuple[str, str]:
    """Pick lower-energy ordering, mirroring bench
    tools/gt_selfrun/m3/m3_run.py:_decide_magnetic_ground_state. On a single
    failure use the survivor; on total failure default FM (collinear up)."""
    if fm_E is not None and afm_E is not None:
        return ("AFM", "energy_comparison") if afm_E < fm_E else ("FM", "energy_comparison")
    if fm_E is not None:
        return ("FM", "afm_failed")
    if afm_E is not None:
        return ("AFM", "fm_failed")
    return ("FM", "both_failed_default")
```

> NOTE: signature uses keyword-only args; the Task 4 test calls `select_ground_state(fm_E=..., afm_E=...)`. Keep this consistent everywhere it is called (Task 6 driver).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetism.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/_magnetism.py tests/test_magnetism.py
git commit -m "feat(v4.3): energy parse + ground-state selection (S2, mirrors bench)"
```

---

## Task 5: Ground-state dataclass + handoff block rendering

**Files:**
- Modify: `agent/subagents/_magnetism.py`
- Test: `tests/test_magnetism.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_magnetism.py  (append)
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

def test_render_block_empty_when_not_ok():
    assert render_magnetic_block(MagneticGroundState(ok=False)) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetism.py -k render_block -q`
Expected: FAIL with `ImportError: cannot import name 'MagneticGroundState'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/_magnetism.py  (append)

@dataclass
class MagneticGroundState:
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

    def to_json(self) -> str:
        import json
        from dataclasses import asdict
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def render_magnetic_block(gs: MagneticGroundState) -> str:
    """Text injected at the top of the main solver + vc_relax prompts.
    Empty when the probe did not produce a usable ground state."""
    if not gs.ok:
        return ""
    mag_lines = "\n".join(
        f"  starting_magnetization({sp}) = {val}"
        for sp, val in gs.starting_magnetization.items()
    )
    return (
        f"## MAGNETIC GROUND STATE (probe) — FIX-32\n\n"
        f"This material is magnetic. A deterministic FM-vs-AFM single-point SCF "
        f"probe selected **{gs.ordering}** as the ground state "
        f"(reason={gs.reason}; FM={gs.fm_energy_ry} Ry, AFM={gs.afm_energy_ry} Ry). "
        f"The bench GT is PBE + nspin=2 (NO Hubbard U); match it.\n\n"
        f"USE in EVERY pw.x run (vc-relax + SCF + bands NSCF + DOS NSCF):\n"
        f"- `nspin = {gs.nspin}` in &SYSTEM (REQUIRED — do not drop it on any step).\n"
        f"- This exact ATOMIC_SPECIES block (note the AFM sublattice split if present;"
        f" the split labels share the same UPF):\n"
        f"```\nATOMIC_SPECIES\n{gs.atomic_species_block}\n```\n"
        f"- These starting_magnetization seeds (assign each split label to the same "
        f"sites the probe used; alternate signs define the AFM sublattices):\n"
        f"```\n{mag_lines}\n```\n"
        f"Keep `mixing_beta = 0.3` and `electron_maxstep = 200` for SCF stability. "
        f"Do NOT apply Hubbard U / lda_plus_u.\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetism.py -q`
Expected: PASS (all tasks 1–5)

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/_magnetism.py tests/test_magnetism.py
git commit -m "feat(v4.3): magnetic ground-state dataclass + handoff block (S2)"
```

---

## Task 6: The magnetic_probe driver (subprocess orchestration)

**Files:**
- Create: `agent/subagents/magnetic_probe.py`
- Test: `tests/test_magnetic_probe.py`

This is the only module that touches the filesystem / runs `pw.x`. It is structured so the *decision logic* is testable with a mocked runner (the actual `pw.x` call is injected).

- [ ] **Step 1: Write the failing test (mocked pw.x runner)**

```python
# tests/test_magnetic_probe.py
from pathlib import Path
from agent.subagents import magnetic_probe as mp
from agent.subagents._magnetism import MagneticProfile

_PROMPT = open("tests/fixtures/nio_prompt.txt").read() if Path("tests/fixtures/nio_prompt.txt").exists() else """For NiO (rocksalt, space group Fm-3m), relax the cell.

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
  Ni: ni.UPF
  O: o.UPF
"""

def test_probe_picks_fm_when_fm_energy_lower(tmp_path, monkeypatch):
    # Inject a fake pw.x runner: FM lower than AFM -> FM wins (NiO case)
    energies = {"FM": -1502.0179532, "AFM": -1502.01004008}
    def fake_run(input_text, out_path, **kw):
        ordering = "AFM" if "AFM" in str(out_path) or "afm" in str(out_path) else "FM"
        out_path.write_text(f"!    total energy =   {energies[ordering]} Ry\nJOB DONE.\n")
        return 0
    monkeypatch.setattr(mp, "_run_pw_x", fake_run)
    gs = mp.run(original_prompt=_PROMPT, workdir=tmp_path, pw_x_path="/bin/true",
                qe_backend="cpu", is_magnetic=True, magnetic_elements=["Ni"],
                seed={"Ni": 0.5})
    assert gs.ok is True
    assert gs.ordering == "FM"
    assert gs.reason == "energy_comparison"
    assert "Ni" in gs.atomic_species_block
    assert (tmp_path / ".magnetic_ground_state.json").exists()

def test_probe_not_magnetic_returns_not_ok(tmp_path):
    gs = mp.run(original_prompt="garbage", workdir=tmp_path, pw_x_path="/bin/true",
                qe_backend="cpu", is_magnetic=False, magnetic_elements=[], seed={})
    assert gs.ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_magnetic_probe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.subagents.magnetic_probe'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagents/magnetic_probe.py
"""Deterministic FM-vs-AFM magnetic ground-state probe (S2).

Runs two single-point spin-polarized SCFs (FM and AFM) on the initial
structure, compares total energies, and returns the lower-energy ordering
plus the ready-to-use ATOMIC_SPECIES + starting_magnetization. Mirrors the
bench's tools/gt_selfrun/m3/m3_run.py:_decide_magnetic_ground_state, so the
solver matches the GT methodology without seeing magnetic_metadata.

Only runs for magnetic materials (gated by config_advisor.is_magnetic), so
M1/M2 nonmagnetic tasks are unaffected.
"""
from __future__ import annotations

import subprocess
import traceback
from pathlib import Path

from ._magnetism import (
    MagneticGroundState,
    MagneticProfile,
    build_probe_scf_input,
    parse_initial_structure,
    parse_total_energy,
    select_ground_state,
)

try:
    from qe_tools import build_subprocess_env  # type: ignore
except Exception:  # pragma: no cover - fallback if import path differs
    def build_subprocess_env() -> dict:  # type: ignore
        import os
        return dict(os.environ)


def _run_pw_x(input_text: str, out_path: Path, *, pw_x_path: str,
              qe_backend: str, cwd: Path, timeout_s: int) -> int:
    """Run pw.x once. Overridden in tests. Returns the process return code."""
    in_path = out_path.with_suffix(".in")
    in_path.write_text(input_text)
    np = 4 if qe_backend == "cpu" else 1
    cmd = ["mpirun", "--allow-run-as-root", "-np", str(np), pw_x_path,
           "-in", in_path.name]
    with out_path.open("w") as fh:
        proc = subprocess.run(cmd, cwd=str(cwd), stdout=fh,
                              stderr=subprocess.STDOUT, env=build_subprocess_env(),
                              timeout=timeout_s)
    return proc.returncode


def _energy_for(ordering: str, *, struct, profile: MagneticProfile, subwork: Path,
                pw_x_path: str, qe_backend: str, timeout_s: int) -> "float | None":
    prefix = f"probe_{ordering.lower()}"
    text = build_probe_scf_input(struct=struct, profile=profile, ordering=ordering,
                                 prefix=prefix)
    out_path = subwork / f"{prefix}.out"
    try:
        _run_pw_x(text, out_path, pw_x_path=pw_x_path, qe_backend=qe_backend,
                  cwd=subwork, timeout_s=timeout_s)
    except Exception:
        traceback.print_exc()
        return None
    if not out_path.is_file():
        return None
    return parse_total_energy(out_path.read_text(encoding="utf-8", errors="replace"))


def run(*, original_prompt: str, workdir: Path, pw_x_path: str,
        qe_backend: str = "gpu", is_magnetic: bool,
        magnetic_elements: list[str], seed: dict[str, float],
        timeout_s: int = 1200) -> MagneticGroundState:
    """Probe the magnetic ground state. Returns ok=False (no-op) when the
    material is nonmagnetic or the structure cannot be parsed — caller then
    skips magnetic handling entirely."""
    if not is_magnetic:
        return MagneticGroundState(ok=False, failure_reason="not magnetic")

    struct = parse_initial_structure(original_prompt)
    if struct is None:
        return MagneticGroundState(ok=False, failure_reason="could not parse structure")

    profile = MagneticProfile(is_magnetic=True, magnetic_elements=magnetic_elements,
                              seed=seed)
    workdir = Path(workdir).resolve()
    subwork = workdir / "magnetic_probe"
    subwork.mkdir(parents=True, exist_ok=True)

    fm_E = _energy_for("FM", struct=struct, profile=profile, subwork=subwork,
                       pw_x_path=pw_x_path, qe_backend=qe_backend, timeout_s=timeout_s)
    afm_E = _energy_for("AFM", struct=struct, profile=profile, subwork=subwork,
                        pw_x_path=pw_x_path, qe_backend=qe_backend, timeout_s=timeout_s)

    ordering, reason = select_ground_state(fm_E=fm_E, afm_E=afm_E)

    # Rebuild the winning input's ATOMIC_SPECIES + signed starting_magnetization
    # so the handoff block matches exactly what scored lowest.
    winning_text = build_probe_scf_input(struct=struct, profile=profile,
                                         ordering=ordering, prefix="probe")
    species_block = _extract_block(winning_text, "ATOMIC_SPECIES", "CELL_PARAMETERS")
    start_mag = _extract_start_mag(winning_text, struct, profile, ordering)

    gs = MagneticGroundState(
        ok=(fm_E is not None or afm_E is not None),
        ordering=ordering, nspin=2,
        magnetic_elements=magnetic_elements,
        atomic_species_block=species_block,
        starting_magnetization=start_mag,
        fm_energy_ry=fm_E, afm_energy_ry=afm_E, reason=reason,
        failure_reason=None if (fm_E is not None or afm_E is not None) else "both SCFs failed",
    )
    (workdir / ".magnetic_ground_state.json").write_text(gs.to_json())
    return gs


def _extract_block(text: str, start_header: str, end_header: str) -> str:
    lines = text.splitlines()
    out, capture = [], False
    for ln in lines:
        if ln.strip() == start_header:
            capture = True
            continue
        if capture and ln.strip().startswith(end_header):
            break
        if capture and ln.strip():
            out.append(ln.strip())
    return "\n".join(out)


def _extract_start_mag(text: str, struct, profile, ordering) -> dict[str, float]:
    """Map species label -> signed seed for the handoff block."""
    from ._magnetism import _afm_labels
    if ordering == "AFM":
        labels = _afm_labels(struct.sites, profile.magnetic_elements)
    else:
        labels = [s.element for s in struct.sites]
    species: list[str] = []
    for lab in labels:
        if lab not in species:
            species.append(lab)
    import re as _re
    result: dict[str, float] = {}
    for sp in species:
        m = _re.match(r"^([A-Z][a-z]?)\d*$", sp)
        el = m.group(1) if m else sp
        if el in profile.seed:
            val = profile.seed[el]
            if ordering == "AFM" and sp.endswith("2"):
                val = -val
            result[sp] = val
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_magnetic_probe.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/magnetic_probe.py tests/test_magnetic_probe.py
git commit -m "feat(v4.3): deterministic FM/AFM magnetic ground-state probe driver (S2)"
```

---

## Task 7: Add magnetic fields to ConfigAdvice + wire classify_magnetism

**Files:**
- Modify: `agent/subagents/config_advisor.py` (dataclass ~48-89; `_deterministic_classify` ~223-240; `_validate_payload` return ~504-528)
- Test: `tests/test_config_advisor.py` (existing module) or `tests/test_magnetism.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_advisor.py  (append; create if absent)
from agent.subagents.config_advisor import _deterministic_classify

def test_classify_marks_elemental_iron_magnetic():
    det = _deterministic_classify("For Fe (bcc, space group Im-3m).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is True
    assert det["magnetic_elements"] == ["Fe"]
    assert det["recommend_nspin"] == 2

def test_classify_marks_cuo_magnetic_even_though_not_in_old_allowlist():
    det = _deterministic_classify("For CuO (tenorite).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is True
    assert det["recommend_nspin"] == 2

def test_classify_nonmagnetic_unchanged():
    det = _deterministic_classify("For Si (diamond).\nPseudopotentials: GBRV_USPP_PBE_v1.5")
    assert det["is_magnetic"] is False
    assert det["recommend_nspin"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_advisor.py -k magnetic -q`
Expected: FAIL with `KeyError: 'is_magnetic'`

- [ ] **Step 3: Implement — three edits to `config_advisor.py`**

3a. Import + add dataclass fields (after `cutoff_sensitive_metal` line ~87):

```python
# top imports (add to the existing `from ._metals import (...)` group)
from ._magnetism import classify_magnetism
```

```python
# in ConfigAdvice dataclass, after `cutoff_sensitive_metal: bool = False`
    # FIX-32 (2026-05-27): composition-based magnetism (replaces the narrow
    # MAGNETIC_TM_OXIDES allowlist for the nspin decision). The winning
    # ordering + seeds come from the magnetic_probe (S2); set here only the
    # classification (is_magnetic + which elements + |seed|).
    is_magnetic: bool = False
    magnetic_elements: list[str] = field(default_factory=list)
    starting_magnetization: dict[str, float] = field(default_factory=dict)
```

3b. In `_deterministic_classify`, before the `return {`  (~line 222), add:

```python
    # FIX-32: composition-based magnetism. is_magnetic if any open-shell
    # 3d/4f element is present. Forces nspin=2 for the whole magnetic class
    # (not just the 10-oxide allowlist). Existing magnetic_tm_oxide path is
    # subsumed: any magnetic material gets smearing + nspin=2 here.
    mag_profile = classify_magnetism(elements)
    if mag_profile.is_magnetic:
        recommend_nspin = 2
        occupations = "smearing"
        # Keep the d-electron-friendly smearing the FIX-19 oxides used; for
        # magnetic metals the METAL_ELEMENTS path already chose mv/0.02 — keep
        # whichever is already set, only ensure smearing is on.
        if smearing is None:
            smearing = "gauss"
            degauss = 0.01
```

And add to the returned dict (~line 239):

```python
        "is_magnetic": mag_profile.is_magnetic,
        "magnetic_elements": mag_profile.magnetic_elements,
        "starting_magnetization": dict(mag_profile.seed),
```

3c. In `_validate_payload`'s `return ConfigAdvice(...)` (~line 504), add:

```python
        is_magnetic=bool(det.get("is_magnetic", False)),
        magnetic_elements=list(det.get("magnetic_elements", [])),
        starting_magnetization=dict(det.get("starting_magnetization", {})),
```

Also ensure `field` is imported (it already is: `from dataclasses import asdict, dataclass, field`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_advisor.py -k magnetic -q && pytest tests/test_magnetism.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/subagents/config_advisor.py tests/test_config_advisor.py
git commit -m "feat(v4.3): ConfigAdvice carries composition-based magnetism (S1, FIX-32)"
```

---

## Task 8: qe_input_lint Rule F — nspin=2 backstop for magnetic materials

**Files:**
- Modify: `agent/tools/qe_input_lint.py` (Rule D region ~434-520; `lint_pw_input` ~631-705)
- Test: `tests/test_qe_input_lint.py` (existing) — add Rule F cases

Rule F is the defense-in-depth net for the most common LLM omission: forgetting `nspin=2` on a step. It reads `.config_advice.json` (via the existing `_find_config_advice`); if `is_magnetic` is true and the input has `nspin=1` or no `nspin`, it auto-injects `nspin = 2` into `&SYSTEM`. It does NOT try to synthesize `starting_magnetization` or species-splits (too invasive) — it warns if `starting_magnetization` is absent so the agent fixes the seeds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qe_input_lint.py  (append)
import json
from pathlib import Path
from agent.tools.qe_input_lint import lint_pw_input

_MAG_INPUT = """&CONTROL
  calculation = 'scf'
/
&SYSTEM
  ibrav = 0
  nat = 2
  ntyp = 1
  ecutwfc = 60
  ecutrho = 600
/
&ELECTRONS
/
ATOMIC_SPECIES
Fe 55.845 fe.UPF
"""

def test_rule_f_injects_nspin_for_magnetic(tmp_path):
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": True, "is_magnetic": True,
        "magnetic_elements": ["Fe"], "occupations": "smearing"}))
    f = tmp_path / "scf.in"
    f.write_text(_MAG_INPUT)
    res = lint_pw_input(f)
    assert "nspin = 2" in f.read_text()        # auto-injected
    assert any("nspin" in r.lower() for r in res.reasons)

def test_rule_f_noop_for_nonmagnetic(tmp_path):
    (tmp_path / ".config_advice.json").write_text(json.dumps({
        "ok": True, "is_metal": False, "is_magnetic": False}))
    f = tmp_path / "scf.in"
    f.write_text(_MAG_INPUT)
    lint_pw_input(f)
    assert "nspin = 2" not in f.read_text()     # untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_qe_input_lint.py -k rule_f -q`
Expected: FAIL (nspin not injected)

- [ ] **Step 3: Implement**

Add a Rule F function near Rule D (after ~line 520):

```python
def _rule_f_magnetic_nspin(input_path: Path, text: str) -> tuple[str, list[str]]:
    """Rule F (FIX-32): when config_advisor flags the material magnetic,
    ensure nspin=2 is present in &SYSTEM (auto-inject if missing/=1). Warn
    if starting_magnetization is absent (cannot safely synthesize it)."""
    advice = _find_config_advice(input_path.parent)
    if not advice or not advice.get("is_magnetic"):
        return text, []
    reasons: list[str] = []
    has_nspin2 = re.search(r"nspin\s*=\s*2", text)
    has_nspin1 = re.search(r"nspin\s*=\s*1", text)
    if has_nspin1:
        text = re.sub(r"nspin\s*=\s*1", "nspin = 2", text)
        reasons.append("Rule F: magnetic material — rewrote nspin=1 → nspin=2")
    elif not has_nspin2:
        # inject right after the &SYSTEM line
        text = re.sub(r"(&SYSTEM\s*\n)", r"\1    nspin = 2\n", text, count=1)
        reasons.append("Rule F: magnetic material — injected nspin = 2 into &SYSTEM")
    if not re.search(r"starting_magnetization", text):
        reasons.append("Rule F WARN: magnetic material but no starting_magnetization "
                       "set — use the MAGNETIC GROUND STATE block seeds.")
    return text, reasons
```

Wire it into `lint_pw_input` (after the ecutrho rewrite / before final write, ~line 678). Follow the existing pattern where Rule C rewrites bytes:

```python
    # Rule F (FIX-32): magnetic nspin backstop. Runs after Rule C so it sees
    # the (possibly ecutrho-fixed) text, and writes the merged result.
    text, f_reasons = _rule_f_magnetic_nspin(input_path, text)
    if f_reasons:
        reasons.extend(f_reasons)
        changed = True   # use whatever flag the function uses to trigger the rewrite
```

> IMPLEMENTATION NOTE: match the variable names actually used in `lint_pw_input` (it tracks the working `text` and a "did we change bytes" flag, then writes `input_path.write_text(text)` at the end). Inspect lines ~652-705 and slot Rule F into that flow; do not introduce a parallel write.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_qe_input_lint.py -k rule_f -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/tools/qe_input_lint.py tests/test_qe_input_lint.py
git commit -m "feat(v4.3): qe_input_lint Rule F nspin=2 backstop for magnetic (S1)"
```

---

## Task 9: Wire the probe into solve.py Phase-1 + inject the handoff

**Files:**
- Modify: `solve.py` (Phase-1 preflight region ~287-369)

The probe runs after `config_advisor` (it needs `advice.is_magnetic` / `magnetic_elements` / seeds) and before `vc_relax` (so the relax uses the chosen ordering). Its rendered block is prepended to `config_advice_block` so it flows into both `vc_relax` (already receives `config_advice_block`) and the main executor prompt (which receives `config_advice_block`).

- [ ] **Step 1: Add the probe call after config_advisor (after ~line 311, before the vc_relax block at ~313)**

```python
    # ─── Magnetic ground-state probe (FIX-32, S2) ───
    # Deterministic FM-vs-AFM single-point SCF dual-probe, mirroring the
    # bench GT methodology. Runs only for magnetic materials, so M1/M2 are
    # unaffected. The chosen ordering + ATOMIC_SPECIES + starting_magnetization
    # are prepended to config_advice_block (flows into vc_relax + main loop).
    if advice is not None and advice.is_magnetic and not os.getenv("DFT_DISABLE_MAGNETIC_PROBE"):
        from agent.subagents import magnetic_probe as magnetic_probe_sa
        print("[solve] magnetic material → spawning FM/AFM ground-state probe", file=sys.stderr)
        harness_wall_s = int(os.environ.get("SOLVER_HARNESS_WALL_S", "14400"))
        probe_budget_s = max(300, int(harness_wall_s * 0.10))  # ≤10% wall for 2 single-points
        try:
            gs = magnetic_probe_sa.run(
                original_prompt=prompt_text, workdir=workdir,
                pw_x_path=pw_x_path, qe_backend=qe_backend,
                is_magnetic=advice.is_magnetic,
                magnetic_elements=advice.magnetic_elements,
                seed=advice.starting_magnetization,
                timeout_s=probe_budget_s,
            )
            if gs.ok:
                mag_block = magnetic_probe_sa._magnetism.render_magnetic_block(gs) \
                    if hasattr(magnetic_probe_sa, "_magnetism") else None
                from agent.subagents._magnetism import render_magnetic_block
                mag_block = render_magnetic_block(gs)
                config_advice_block = mag_block + "\n" + config_advice_block
                print(f"[solve] magnetic probe → ordering={gs.ordering} "
                      f"(FM={gs.fm_energy_ry} AFM={gs.afm_energy_ry} Ry, {gs.reason})",
                      file=sys.stderr)
            else:
                print(f"[solve] magnetic probe not usable: {gs.failure_reason} "
                      f"→ falling back to advisory-only magnetism", file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            print(f"[solve] magnetic probe crashed; continuing: {e}", file=sys.stderr)
```

> CLEANUP: the `hasattr(... "_magnetism")` line above is redundant — keep only the direct `from agent.subagents._magnetism import render_magnetic_block; mag_block = render_magnetic_block(gs)`. (Listed here so the worker removes the stray line.)

- [ ] **Step 2: Verify it imports + runs end-to-end on a nonmagnetic task (no regression)**

Run a dry parse-only smoke (no QE needed) to confirm `solve.py` still imports and the probe is skipped for nonmagnetic:

```bash
DFT_DISABLE_CONFIG_ADVISOR=1 python -c "import solve; print('solve imports OK')"
python -m py_compile solve.py agent/subagents/magnetic_probe.py agent/subagents/_magnetism.py
```
Expected: `solve imports OK`, no compile errors.

- [ ] **Step 3: Commit**

```bash
git add solve.py
git commit -m "feat(v4.3): wire magnetic ground-state probe into Phase-1 preflight (S2)"
```

---

## Task 10: Full regression + devlog

**Files:**
- Modify: `devlog.md`

- [ ] **Step 1: Run the whole suite**

Run: `/path/to/projects/solver/v4.2/.venv/bin/python -m pytest -q`
Expected: previous baseline (611 passed + 3 pre-existing pymatgen sg failures) PLUS the new magnetism/probe/lint tests passing; **no new failures**. Record the count.

- [ ] **Step 2: Add a devlog entry** (top of `devlog.md`, follow existing format, `[uncommitted]` until committed)

```
- **v4.3** · 2026-05-27 HH:MM · [uncommitted] feat(v4.3): M3 magnetism S1+S2 (FIX-32) — composition-based magnetic classification (`agent/subagents/_magnetism.py`: open-shell 3d/4f elements, replaces the 10-formula MAGNETIC_TM_OXIDES allowlist for the nspin decision) + deterministic FM-vs-AFM single-point-SCF dual-probe (`magnetic_probe.py`, mirrors bench `_decide_magnetic_ground_state`) wired into solve.py Phase-1 before vc_relax; ConfigAdvice carries is_magnetic/magnetic_elements/starting_magnetization; qe_input_lint Rule F auto-injects nspin=2 for magnetic inputs. Targets M3 batch 379913…baf1 (v4.2 scored 0.41): Fe_bcc/Cr_bcc/CuO/Fe2O3 (no nspin → moment 0) and NiO (textbook AFM vs GT PBE-FM). Probe gated on is_magnetic → M1/M2 unaffected. +N tests, full suite XXX passed. NOT done here: S3 (carry magnetic state into bands/DOS) + S4 (spin-aware T2 extraction) — T2 bands_dos still expected low until those land.
```

- [ ] **Step 3: Commit**

```bash
git add devlog.md
git commit -m "docs(v4.3): devlog M3 magnetism S1+S2 (FIX-32)"
```

---

## Verification (end-to-end, requires QE + a magnetic task)

1. **Unit**: `pytest tests/test_magnetism.py tests/test_magnetic_probe.py tests/test_config_advisor.py -q` — all green.
2. **No regression**: full `pytest -q` matches the pre-change baseline + new tests.
3. **Probe correctness on the known flip (NiO)**: run the solver on `M3_T1_NiO_vc_relax` with QE available and confirm in `workdir/.magnetic_ground_state.json` that `ordering="FM"` and `fm_energy_ry < afm_energy_ry` (GT is FM). Confirm the final `vc-relax.in` contains `nspin = 2` and the FM `starting_magnetization`. Expected: `total_magnetization_per_cell` near GT 6.14 (was 0).
4. **No-op on nonmagnetic**: run on an M1 task (e.g. `M1_T1_Si_vc_relax`); confirm `[solve] magnetic material` does NOT print, no `magnetic_probe/` dir, score unchanged.
5. **Backstop**: hand-write a magnetic SCF input lacking `nspin`, run `lint_pw_input`; confirm `nspin = 2` injected.
6. **Re-run the M3 T1 subset** (the 15 `M3_T1_*` tasks) via a batch and compare T1 magnetization sub-scores against batch `379913…baf1`. Success target: the six T1 tasks that scored magnetization=0 (Fe_bcc, Cr_bcc, CuO, Fe2O3, FeO, NiO) recover most of the 35% magnetization weight; no previously-passing T1 task (CoO, MnO, CrO2, Ni_fcc, Co_hcp) regresses.

---

## Scope boundaries (explicit)

**In scope (this plan):** S1 (magnetic classification + nspin enforcement) and S2 (FM/AFM dual-probe). These primarily recover **T1 vc_relax** magnetization + the lattice/space-group consequences of correct ordering.

**NOT in scope (follow-up plans):**
- **S3** — carry the magnetic state (nspin + per-site starting_magnetization) through vc_relax→SCF→bands→DOS handoffs; surface converged moments. (T2 needs this.)
- **S4** — spin-aware T2 extraction: robust eigenvalue parse under nspin=2 (avoid the bands.x "constrained magnetization not tested" failure that caused 8/8-null submissions), spin-summed per-formula-unit DOS, deterministic metal reporting.
- T2 bands_dos scores will stay low until S3+S4. Do not expect this plan to move T2.

## Bench-side issues to report (do NOT modify bench)

1. **Methodology confirmation**: since `material.magnetic_metadata` is hidden from the solver, confirm with the owner that the solver is *intended* to rediscover the FM/AFM ground state by its own probe (this plan assumes yes). If the bench would rather expose a hint, the probe could be simplified.
2. **Fe3O4 T1 `extraction failed: list index out of range`** (`bench/harness/lib/evaluator.py extract_t1_fields`) — likely fragile parsing when species labels are non-standard (agent used `Fe_A`/`Fe_B`). This plan sidesteps it (probe emits `Fe1`/`Fe2`), but the evaluator fragility remains.
3. **Cr2O3 T1 `space_group_number=0` despite correct AFM** — check whether `extract_t1_fields`' pymatgen `SpacegroupAnalyzer` treats AFM-split labels (`Cr1`/`Cr2`) as distinct elements, lowering detected symmetry. GT (also AFM) scored 167, so confirm extraction parity between GT generation and the agent path.

---

## Self-Review notes (run after implementation)

- **Spec coverage**: S1 → Tasks 1,7,8. S2 → Tasks 2,3,4,5,6,9. "Not overfit" → Task 1 (chemistry set), Task 4/6 (replicate bench probe). "Don't touch bench" → no bench edits; issues listed above. ✓
- **Type consistency**: `MagneticProfile(is_magnetic, magnetic_elements, seed)`, `MagneticGroundState(ok, ordering, nspin, magnetic_elements, atomic_species_block, starting_magnetization, fm_energy_ry, afm_energy_ry, reason, failure_reason)`, `select_ground_state(*, fm_E, afm_E)`, `build_probe_scf_input(*, struct, profile, ordering, …)` — used identically across Tasks 3/4/5/6. ✓
- **Placeholder scan**: the two flagged stray lines (Task 8 `changed` flag name, Task 9 `hasattr` redundancy) are called out for the worker to reconcile against the real code; no `TODO`/`TBD` in shipped code.

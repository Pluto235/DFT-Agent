# Bench-side findings — M3_T2 band-structure extraction (2026-05-29)

**Status: REPORT ONLY.** Per `solver/CLAUDE.md` the solver side does not modify
the bench. These are bench-side issues found while diagnosing v4.3 M3 runs; they
need the bench owner's decision. Solver-side mitigations already landed (FIX-34)
are noted where relevant.

---

## Finding 1 (primary) — evaluator seekpath crashes on AFM-split species labels

**Where:** `bench/harness/lib/evaluator.py:_build_seekpath_input` (~line 457):

```python
numbers.append(int(Element(site["element"]).Z))
```

**What:** 8 M3_T2 task definitions ship AFM sublattice labels (`Co1`, `Cr1`,
`Cu1`, `Fe1`, `Mn1`, …) directly in
`material.initial_structure.atomic_positions_crystal`:

| task | labels |
|------|--------|
| M3_T2_CoO / Cr2O3 / Cr_bcc / CuO / Fe2O3_hematite / Fe3O4_magnetite / MnF2 / MnO | `<El>1`, `<El>2` |

`pymatgen.core.Element("Co1")` raises `ValueError: 'Co1' is not a valid Element`.
`extract_t2_fields` wraps the seekpath call in `try/except Exception` and on
failure returns `_none_t2_result_partial(...)` → for a smeared (fermi-mode)
bands run **`band_gap_ev`, `gap_type`, `vbm_k_label`, `cbm_k_label` are all
null**. The solver never touches `initial_structure`, so it cannot avoid this.

**Why only magnetic insulators show it:** true metals (CrO2, Fe_bcc, Cr_bcc)
hit the metal short-circuit (`_metal_t2_result`) before the seekpath block, so
the bad label never bites them. Only the insulating AFM oxides reach seekpath.

**Reproduction** (read-only):

```python
# tritondft env
import json, sys; sys.path.insert(0, "/path/to/project/bench")
from harness.lib.evaluator import _build_seekpath_input
import seekpath
t = json.load(open("bench/questions/M3_T2/M3_T2_CoO_bands_dos.json"))
seekpath.get_path(_build_seekpath_input(t["material"]["initial_structure"]))
# → ValueError: 'Co1' is not a valid Element
```

**Score impact:** `gap_type` (0.15) + `vbm_k_label` (0.10) + `cbm_k_label`
(0.10) = **0.35 weight nulled** on the 7 affected insulators (Cr_bcc excluded —
metal). `band_gap_ev` (0.40) is also nulled *in fermi-mode*, but the solver's
FIX-34 (fixed occupations on the bands NSCF) makes QE emit the HOMO/LUMO line so
the evaluator computes `band_gap_ev` **before** the seekpath step (verified:
CoO recovers `band_gap_ev = 0.003`). gap_type + labels remain bench-blocked.

**Suggested bench fix (owner's call):** normalise the species label to its base
element before `Element(...)` in `_build_seekpath_input`, e.g.
`re.match(r"[A-Z][a-z]?", site["element"]).group(0)`. (This mirrors what the
solver's `parse_initial_structure` now does — FIX-36.) Alternatively store a
clean element symbol + a separate magnetic-sublattice field in the task data.

---

## Finding 2 (secondary) — `parse_bands_dat` reads spin-up only for nspin=2

**Where:** `bench/harness/lib/evaluator.py:parse_bands_dat` (~289-337).

The `&plot nbnd=N, nks=M /` header reports the per-spin k-count. For an nspin=2
`bands.dat`, `bands.x` writes spin-up then spin-down (2·M k-blocks). The parser
loops `for i in range(nks)` over the first `M` blocks only → it silently uses
**spin-up eigenvalues only** and ignores spin-down.

**Impact:** harmless for AFM systems (spin channels are degenerate), but for a
ferromagnetic metal/half-metal the minority-spin band edge could be missed,
giving a wrong VBM/CBM or gap. Low priority for the current M3 set (the affected
insulators are AFM), but worth a guard if FM half-metals enter the suite.

**Suggested bench fix (owner's call):** detect the doubled body
(`len(floats) == 2·M·(3+N)`) and either fold both spins into the per-k band list
or expose both channels to the VBM/CBM search.

---

## Finding 3 — T1 `_parse_positions` keeps AFM split labels → wrong space group

**Where:** `bench/harness/lib/evaluator.py:_parse_positions` (~line 159) →
`extract_t1_fields` (~line 52-64):

```python
species.append(parts[0])                       # keeps raw "Cr1" / "Cr2"
...
structure = Structure(lattice, species, frac_coords, ...)
sga = SpacegroupAnalyzer(structure, symprec=..., angle_tolerance=...)
```

**What:** the T1 analog of Finding 1. A magnetic vc-relax must label sublattices
`Cr1`/`Cr2` so QE can assign opposite `starting_magnetization`. The extractor
keeps the raw label, so `SpacegroupAnalyzer` treats `Cr1 != Cr2` as distinct
species and returns a **subgroup** instead of the crystallographic group the GT
records.

**Proven** (re-running the bench extractor on the real relaxed outputs, raw vs.
digit-stripped labels):

| material | raw label → sg | stripped → sg | GT sg |
|----------|----------------|---------------|-------|
| Cr2O3    | 148 (R-3)      | **167 (R-3c)**| 167   |
| Fe2O3    | 161 (R3c)      | **167**       | 167   |
| MnF2     | 65 (Cmmm)      | **136 (P4₂/mnm)** | 136 |
| Cr_bcc   | 221 (Pm-3m)    | **229 (Im-3m)**   | 229 |

Stripping recovers the exact GT and **regresses none** of the other 11 M3 T1
materials (CoO/MnO reach sg=166 from the physical rhombohedral distortion either
way; FeO/LaMnO3/Fe3O4 are wrong for unrelated physics reasons, unchanged).

**Score impact:** `space_group_number` (0.10) + `point_group` (0.05) [+ `crystal_system`
(0.05) for MnF2] per affected task — **≈ 0.60 aggregate** over the 4 tasks.

**Suggested bench fix (owner's call):** strip the trailing digit from the species
label before building the `Structure` (element-based symmetry is what the GT
uses), or pass magnetic-aware symmetry. **Worked around solver-side by FIX-37**
(the solver merges `Cr1/Cr2 → Cr` in the `ATOMIC_POSITIONS` of the submitted
vc-relax.out), so this is informational unless the owner prefers a bench fix.

---

## Finding 4 — `read_dos_dat` reads spin-up only for nspin=2 → half the DOS

**Where:** `bench/harness/lib/evaluator.py:read_dos_dat` (~line 373):

```python
dos.append(vals[1])     # col 1 = dosup for an nspin=2 dos.x file
```

**What:** for nspin=2, QE's `dos.x` writes `E  dosup  dosdw  intdos` (4 columns).
The parser takes `vals[1]` = **dosup only** = half the total DOS, while the
`dos_curve` reference (`questions/.../dos_reference.dat`,
`DOS_states_per_eV_per_fu`) is the **total**. Result: `dos_curve` recall caps
near 0.5 on every nspin=2 task.

**Proven** (bench `_score_curve_f1` against the real ±5 eV reference, CrO2):
`col1=dosup` → F1 = **0.647** (matches the reported 0.65), P=0.97 / R=0.48;
`dosup+dosdw` → F1 = **0.945**. (Per-formula-unit division is *not* needed and
makes it worse — the summed per-cell total already matches the reference area.)
12 of 13 M3 T2 dos.dat are 4-column (nspin=2); the lone 3-column case (Ni_fcc)
is a separate issue — its DOS NSCF ran nspin=1.

**Score impact:** `dos_curve` weight 0.20; summing lifts CrO2 0.65→0.945
(+0.30 → +0.06 total). AFM insulators (dosup==dosdw by symmetry) recover
similarly. Aggregate over the 12 nspin=2 tasks is substantial.

**Suggested bench fix (owner's call):** when a dos.dat has ≥4 columns, sum
`dosup+dosdw` for the total DOS. **Worked around solver-side by FIX-38** (the
solver collapses the submitted dos.dat to `E (dosup+dosdw) intdos`).

---

## Solver-side status (for context)
- FIX-34: magnetic-insulator bands NSCF → `occupations='fixed'` + generous
  `nbnd`, recovering `band_gap_ev` ahead of the seekpath step.
- FIX-36: solver `parse_initial_structure` now tolerates `Cr1/Cr2`-style labels
  (the probe was building O-only inputs and MPI_ABORTing before this).
- FIX-37: solver merges AFM split labels (`Cr1/Cr2 → Cr`) in the submitted
  vc-relax.out `ATOMIC_POSITIONS`, working around Finding 3 (T1 sg/pg/cs).
- FIX-38: solver sums nspin=2 spin channels in the submitted dos.dat, working
  around Finding 4 (T2 `dos_curve`).
- Neither solver fix can recover `gap_type`/`vbm_k_label`/`cbm_k_label` for the
  7 AFM insulators — those are gated on Finding 1 (bench-only).

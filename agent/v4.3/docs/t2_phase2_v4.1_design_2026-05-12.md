# T2 Phase 2 (v4.1) Design — Streams A, B, D

**Date**: 2026-05-12
**Status**: Design / spec — pending user review then writing-plans
**Author**: Claude (Opus 4.7) + Shijie

## 1. Context

After v4 Phase 1 (audit fix + diagnose_t2) and Phase 2 P0c+P1 (pymatgen
geometry + bands→nscf rewrite + ecutwfc=80 alignment), AlAs trajectory is:

| Stage      | total | dos_e_fermi | dos_curve | cbm_k_label |
|------------|------:|------------:|----------:|------------:|
| Phase 1    | 0.16  | 0           | 0.81      | 0           |
| P0c        | 0.785 | 0           | 0.67      | 0           |
| P1         | 0.798 | **1.00**    | 0.49      | 0           |
| P1b (sigma)| 0.65  | 0           | 0         | 0           |

P1 hit dos_e_fermi=1.00 by **lucky alignment** between NSCF tetrahedra Fermi
and SCF VBM. P1b's deliberate `sigma=0.02` change triggered a stochastic
NSCF param rewrite (cg + nbnd=20) which broke the alignment.

### Three identified levers (post-brainstorm 2026-05-12)

| Stream | Lever | Why | T1 risk |
|---|---|---|---|
| A | qe_input_lint defensive rules for NSCF | Removes stochastic param variance | Zero (calc mutex) |
| B | rewrite_dos_efermi postprocess | Aligns to bench GT-pipeline convention | Zero (T2 only) |
| D | run_bands_dos_workflow optional super-tool | Optional fully-scripted T2 path | Zero (T2 only) |

The three are **orthogonal** — each delivers value independently; D
internally uses B, but A and B remain useful when LLM does not invoke D.

## 2. Goals

1. **AlAs ≥ 0.85 stable** (P50 across ≥3 runs), via Stream A+B
2. **Workflow shortcut**: when LLM uses Stream D, AlAs ≥ 0.90 deterministic
3. **T1 zero regression**: M1_T1_Si still 1.0
4. **Cross-task generalization**: ≥1 other T2 (Diamond or MgO) shows
   matching score lift

## 3. Stream A — qe_input_lint defensive rules

### A1. `dos_nscf_lint`

**Trigger detection** (both required):
1. pw.x input with `calculation = 'nscf'`
2. K_POINTS uses `automatic` (dense MP grid) — NOT `crystal_b` / `tpiba_b`

These two conditions are sufficient: any NSCF with automatic k-grid in a
T2 context is a DOS-NSCF (bench T2 prompts never ask for non-bands-non-DOS
NSCF). The check is mutually exclusive with A2's `bands_nscf_lint` trigger
(which requires crystal_b / tpiba_b), so no double-patching.

**Auto-patches** (auto-correct + log to `--- qe_input_lint ---` block):
- `diagonalization = 'cg'` → `'david'`
- `occupations = 'smearing'` → `'tetrahedra'` (drop smearing/degauss/ngauss)
- `nbnd`: if > `ceil(1.6 * n_occ + 8)`, warn (do not auto-fix — may be intentional). `n_occ = n_elec/2` for nspin=1; `= n_elec` for nspin=2
- Add `tprnfor = .false., tstress = .false.` if absent (DOS doesn't need them)

### A2. `bands_nscf_lint`

**Trigger detection**:
1. pw.x input with `calculation = 'nscf'`
2. K_POINTS uses `crystal_b` or `tpiba_b`

**Auto-patches**:
- `diagonalization = 'cg'` → `'david'`
- `occupations = 'smearing'/'tetrahedra'` → `'fixed'`
- `nbnd`: if > `ceil(2.0 * n_occ + 4)`, warn (n_occ as defined in A1)

### Implementation

- Extend `agent/tools/qe_input_lint.py` with two new functions
  `_apply_dos_nscf_rule` and `_apply_bands_nscf_rule` mirroring the existing
  `_apply_vc_relax_rule` and `_apply_bands_to_nscf_rule` shape
- Trigger order: rules are tested in sequence; first matching rule "owns"
  the input (prevents double-patching)
- Surface format: same `--- qe_input_lint ---` block already used by
  vc-relax — LLM learns from feedback

### Tests (extend `tests/test_qe_input_lint.py`)

- 6 new tests minimum:
  - DOS NSCF with cg → patched to david
  - DOS NSCF with smearing → patched to tetrahedra
  - bands NSCF (crystal_b) with cg → patched
  - vc-relax input → unaffected (T1 regression gate)
  - scf input → unaffected
  - DOS NSCF with overly-large nbnd → warning surfaced, not patched

### T1 zero-regression

A1 and A2 both require `calculation = 'nscf'`. T1's vc-relax has
`calculation = 'vc-relax'`. The triggers are **mutually exclusive** by
QE's own grammar. Verified via existing `tests/test_qe_input_lint.py::test_lint_skips_scf`
pattern.

## 4. Stream B — `qe_tools.rewrite_dos_efermi(dos_dat, scf_out)`

### Signature

```python
def rewrite_dos_efermi(dos_dat_path: str, scf_out_path: str,
                       tol_ev: float = 1e-6) -> dict:
    """Overwrite dos.dat's '# EFermi = X eV' header with SCF.out's
    highest_occupied level (eV).

    Aligns dos.dat with bench's GT pipeline
    (~/bench/dft-agent-bench/tools/gt_selfrun/t2/t2_extract.py:
    extract_fermi_for_point) which reads SCF.out highest_occupied;
    the bench evaluator (harness/lib/evaluator.py:extract_t2_fields)
    reads dos.dat header. The two diverge on insulators because NSCF
    Fermi (tetrahedra-computed) drifts from SCF VBM by 10–250 meV.

    Idempotent: if existing header is within tol_ev of SCF VBM, no-op.

    Returns:
        {'before': float, 'after': float, 'modified': bool}
    """
```

### Implementation

- Parse SCF.out: scan for line `the highest occupied level is   X.XXXX ev`
  (case-insensitive); also handle `highest occupied, lowest unoccupied`
  variants → use first number
- Parse dos.dat: first line matches `^#\s*E_?Fermi\s*=\s*([0-9.+-]+)\s*eV`
- Replace in-place via read-write (small file ~100KB; safe)
- If SCF.out missing the line (metallic system uses smearing), fall back
  to `Fermi energy is   X.XXXX ev` line
- Edge: if dos.dat header missing, prepend the line (defensive)

### Tests (new `tests/test_rewrite_dos_efermi.py`)

- 5 unit tests:
  - Insulator: SCF VBM 5.27, dos.dat header 5.19 → patched to 5.27, modified=True
  - Already-aligned: SCF 5.27, dos.dat 5.27 → no-op, modified=False
  - Metallic: SCF "Fermi energy is" → uses that value
  - Missing header in dos.dat → prepended
  - Malformed scf.out → raises with clear error

### Integration

- Surface in `agent/system_prompt.py` §T2 final step:
  > "After dos.x completes, call `from qe_tools import rewrite_dos_efermi;
  > rewrite_dos_efermi('dos.dat', 'scf.out')` to align Fermi convention
  > with bench's GT pipeline."

## 5. Stream D — `qe_tools.run_bands_dos_workflow(...)` (optional)

### Signature (D-full)

```python
def run_bands_dos_workflow(
    *,
    structure,                       # pymatgen.Structure
    pseudo_dir: str,
    pseudos: dict[str, str],         # {"Al": "Al.upf", "As": "As.upf"}
    workdir: str = ".",
    ecutwfc: int = 80,
    ecutrho: int = 800,
    kgrid_scf: tuple[int, int, int] = (8, 8, 8),
    kgrid_dos: tuple[int, int, int] = (12, 12, 12),
    nbnd_pad: int = 8,
    pw_cmd: str = "pw.x",
    bands_cmd: str = "bands.x",
    dos_cmd: str = "dos.x",
    nproc: int = 1,
) -> dict:
    """Returns:
        {
          'task_outputs': {
              'scf_out': 'scf.out',
              'bands_out': 'bands.out',
              'bands_data': 'bands.dat',
              'dos_dat': 'dos.dat',
          },
          'task_results': {
              'band_gap_ev': float,
              'gap_type': 'direct' | 'indirect',
              'vbm_k_label': str,      # e.g. 'GAMMA', 'X', 'L'
              'cbm_k_label': str,
              'dos_e_fermi': float,
          },
          'meta': {
              'kpath_labels': [str, ...],
              'is_metallic': bool,
              'n_elec': int,
              'wall_seconds': float,
          },
        }
    """
```

### 7 internal steps

1. **seekpath**: `seekpath.get_path(structure_to_seekpath_tuple(structure))`
   → primitive cell + k-path with labels (e.g., `[("GAMMA", "X"), ("X", "L"), ...]`)
2. **SCF**: `write_pw_input` with `calculation='scf'`, kgrid_scf, ecutwfc/ecutrho;
   `run_pw` (synchronous, parse `scf.out` for n_elec + VBM)
3. **bands-NSCF**: `write_pw_input` with `calculation='nscf'`, `K_POINTS crystal_b`
   from seekpath path, `nbnd = n_occ + nbnd_pad` (n_occ = n_elec/2 in
   default nspin=1; v4.1 workflow assumes nspin=1 — magnetic systems are
   out of scope, see §10), `occupations='fixed'`, `diagonalization='david'`;
   `run_pw` → bands.out
4. **bands.x**: standard `&BANDS` block → bands.dat
5. **DOS-NSCF**: `write_pw_input` with `calculation='nscf'`, automatic kgrid_dos,
   `occupations='tetrahedra'`, `diagonalization='david'`; `run_pw` → dos_nscf.out
6. **dos.x**: standard `&DOS` block (Emin/Emax from band extrema ±5 eV,
   DeltaE=0.01); → dos.dat
7. **Postprocess**:
   - Call `rewrite_dos_efermi('dos.dat', 'scf.out')` (Stream B)
   - Parse bands.out for VBM/CBM eigenvalues + their k-points
   - Match k-points to seekpath labels via tolerance 0.05 (fractional coords)
   - Compute `gap_type`: 'direct' if VBM_k ≈ CBM_k (within 0.05 frac), else 'indirect'
   - Read dos.dat header EFermi → dos_e_fermi
   - Compute band_gap_ev = CBM - VBM

### Tests (new `tests/test_run_bands_dos_workflow.py`)

**Unit-style** (mock pw_cmd / bands_cmd / dos_cmd via fake subprocess):
- Verify SCF input file content (ecutwfc=80, etc.)
- Verify NSCF (bands) input has crystal_b k-path
- Verify NSCF (dos) input has automatic + tetrahedra
- Verify rewrite_dos_efermi is called before reading EFermi
- Verify task_outputs dict keys match `_T2_KEYS`

**Integration-style** (real binary, smoke test):
- AlAs full workflow → score ≥ 0.85 (deferred to manual verification, not pytest)

### Integration in system_prompt

Append at end of §T2:
> **Shortcut**: if you are confident the canonical setup is appropriate
> (PBE GGA, ecutwfc=80, MP grids), import and call:
> ```python
> from qe_tools import run_bands_dos_workflow
> result = run_bands_dos_workflow(
>     structure=structure, pseudo_dir='./pseudos', pseudos={'Al': 'Al.upf', ...})
> # then submit with result['task_results'] as fields, result['task_outputs'] as paths
> ```
> The workflow handles all of: seekpath k-path, SCF→bands→DOS, Fermi alignment,
> VBM/CBM/k_label extraction. You may still use the manual flow above if you
> need custom k-path or non-standard ecutwfc.

## 6. Test plan

### Unit tests (pytest)

- All existing 125 tests still pass (T1 zero regression gate)
- New: ≥6 in `test_qe_input_lint.py` (Stream A)
- New: ≥5 in `test_rewrite_dos_efermi.py` (Stream B)
- New: ≥6 in `test_run_bands_dos_workflow.py` (Stream D unit-style with mocks)

### Smoke tests (real QE on harness)

1. **T1 regression**: M1_T1_Si → must be 1.0 (gate)
2. **Stream A alone** (manual T2): AlAs without workflow → P50 ≥ 0.82
3. **Stream B alone** (manual T2 + Fermi postprocess): AlAs → dos_e_fermi=1.0 stable
4. **Stream D shortcut**: AlAs with workflow → total ≥ 0.90
5. **Cross-task**: Diamond or MgO with workflow → improvement vs baseline

### Order

A → B → D, each with its own commit + verification before the next.

## 7. Rollout

| Commit | Stream | Files touched |
|---|---|---|
| 1 | A1 + A2 | `agent/tools/qe_input_lint.py`, `tests/test_qe_input_lint.py` |
| 2 | B | `qe_tools.py`, `tests/test_rewrite_dos_efermi.py`, `agent/system_prompt.py` |
| 3 | D | `qe_tools.py`, `tests/test_run_bands_dos_workflow.py`, `agent/system_prompt.py` |
| 4 | docs | `docs/t2_phase2_results_2026-05-XX.md` (benchmark report) |

Each commit gates on:
- All pytest green
- M1_T1_Si smoke = 1.0
- ≥1 T2 sample shows expected behavior

## 8. Risks

| Risk | Mitigation |
|---|---|
| seekpath label fallback fails on edge-case cells (e.g., low symmetry) | Fall back to coordinate string `(0.50,0.25,0.00)` if no label within tol; warn user in result['meta'] |
| Metallic systems break Stream B (no `highest_occupied` line in scf.out) | Detect via `is_metallic` flag, fall back to `Fermi energy is` line; if both missing, skip rewrite (return modified=False) |
| Bench changes Fermi convention upstream | Document in code comment + docs/t2_phase2_results.md; Stream B is a thin alignment shim, removable |
| Stream D's pseudos dict mismatch with file system | Validate file existence before SCF call; raise clear error |
| T1 regression from any stream | Each commit verified independently via M1_T1_Si smoke |

## 9. Open questions (defer to implementation)

1. Stream D's metallic-system path: should it switch DOS from tetrahedra
   to smearing automatically based on SCF VBM detection? (probably yes;
   verify on a test metal)
2. Should Stream B also rewrite dos_curve E-axis to be SCF-VBM-referenced?
   (deferred to v4.3 if dos_curve still under-scores)
3. Cache decisions in `meta` dict — should we persist this to a sidecar
   JSON for debug? (yes, write `workflow_meta.json` to workdir)

## 10. Out of scope (NOT in this design)

- T2 multi-sample best-of-N
- Model upgrade haiku→sonnet
- Manifest registration of `supported_task_types: [vc_relax, bands_dos]`
  (deferred to v4.3 after we have ≥3 stable T2 wins)
- Bench-side fixes (Fermi convention alignment in evaluator)
- Magnetic / spin-polarized systems (`nspin=2`, magnetic moments) — Stream D
  workflow assumes `nspin=1`; M1_T2 task pool is all closed-shell. v4.3 if
  needed.

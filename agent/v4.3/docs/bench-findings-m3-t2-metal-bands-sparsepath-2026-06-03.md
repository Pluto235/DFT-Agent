# Bench-side findings — M3 batch d383f69, 2026-06-03

REPORT ONLY. Per the repo `CLAUDE.md` the bench (`/path/to/project/bench/`)
is not ours to edit — this records a bench-scoring weakness found while raising
M3 scores on the solver side, with a suggested fix for the bench owner to weigh.
Continues the Finding numbering from
[`bench-findings-m3-followup-2026-05-31.md`](bench-findings-m3-followup-2026-05-31.md)
(which ended at Finding 7).

---

## Finding 8 — a GT-known metal with a correct DOS is scored a semiconductor when its bands k-path under-samples E_F

**Where:** `bench/harness/lib/evaluator.py:extract_t2_fields` — the fermi-splitter
path (~707–758), `_find_vbm_cbm_via_fermi` (line 523, `eps=5e-3`), the metal
short-circuit (line 757), and `is_metal_per_gt` derivation in
`bench/harness/run.py:180`.

**What:** For a bands NSCF run with smearing (no HOMO/LUMO line → `splitter_mode
= "fermi"`), the evaluator computes the gap by slicing the **submitted** bands.dat
at the bands-NSCF Fermi: `VBM = max(eig ≤ E_F+eps)`, `CBM = min(eig > E_F+eps)`
globally over the k-path, then short-circuits to `gap_type=metal` **only** when
`gap_value < 1e-2 eV` (and `is_metal_per_gt is not False`). A sparse
high-symmetry k-path can leave a finite "void" straddling E_F even for a true
metal — no sampled k-point lands near a Fermi-surface crossing — so the computed
gap exceeds 10 meV and the run is labelled an indirect semiconductor.

This is a **new failure mode, distinct from Findings 1/6** (the AFM-split-label
seekpath crash). Here the material is a **bare-label FM metal** (`Ni_fcc`, single
`Ni` species): there is no seekpath crash, the DOS is correct, and the NSCF
already uses smearing — the *only* problem is k-path density near E_F.

**Evidence (`M3_T2_Ni_fcc_bands_dos`, run `2026-06-02T03-29-15Z`):**
- GT `gap_type="metal"`, `band_gap_ev=0.0` → `is_metal_per_gt=True` (short-circuit
  is enabled).
- Submitted bands: `K_POINTS crystal_b`, 8 vertices × 30 subdivisions/segment,
  nspin=2, smearing. **Zero** eigenvalues within 90 meV of E_F; nearest below =
  17.7950 eV, nearest above = 17.9730 eV → computed gap **0.178 eV** > 10 meV →
  no short-circuit.
- Scored fields: `band_gap_ev=0`, `gap_type=0`, `vbm_k_label=0`, `cbm_k_label=0`
  (only `dos_e_fermi=1.0` survived) → **task total 0.128**.
- The DOS is correct: `dos_e_fermi` extracted = 17.84–17.94 eV, matched GT 17.9044
  (scored 1.0) — i.e. the evaluator already holds enough information to know it is
  a metal, but does not use it for `gap_type`.
- Fermi-frame mismatch ruled out: the void/gap is **identical (0.178 eV)** whether
  sliced at the DOS Fermi (17.846) or the bands Fermi (17.937).
- The void shrinks ≈ linearly with k-path density, so resolving Ni to < 10 meV
  needs ≈ 500 points/segment — a plain density bump is not reliably enough by
  itself. `Fe_bcc`/`Cr_bcc` (bcc) pass only because their broad d-bands happened
  to place a sampled k-point near E_F.

**Solver-side mitigation (FIX-2, this repo):** for an `is_metal` material, the
solver now measures the same void post-hoc (`agent/subagents/metal_bands_guarantee.py`),
and if it exceeds 10 meV deterministically re-runs the bands NSCF on a denser
k-path + bands.x (density sized from the measured void, capped) until an
eigenvalue lands within ~10 meV of E_F, then overwrites the submitted bands.dat.
This makes the bench's existing short-circuit fire. It is a workaround — extra
NSCF wall-clock to satisfy a sparse-path scorer — not a fix of the root cause.

**Suggested bench fix (owner's call) — either is ~1–2 lines and lets the solver
drop the FIX-2 re-run:**
- **(A)** When `is_metal_per_gt is True`, short-circuit to `_metal_t2_result`
  regardless of the sparse-path gap (gate at `evaluator.py:757`). The GT already
  asserts the material is a metal; a sparse bands path can only *manufacture* a
  false gap, never hide a real one, so trusting the GT here is strictly safer.
- **(B)** Cross-check the DOS the evaluator already parses: if `N(E_F) > 0` (finite
  total DOS at the Fermi level from `dos_curve` / `nscf_dos_out`), force
  `gap_type=metal`. Physically robust and independent of k-path density.

**Impact:** every GT-metal whose submitted bands path under-samples E_F is at risk
of the full `band_gap`/`gap_type`/`vbm`/`cbm` collapse (~0.23 of the T2 score per
such task). Among M3_T2 metals this hit `Ni_fcc`; `Fe_bcc`/`Cr_bcc` passed only by
sampling luck.

---

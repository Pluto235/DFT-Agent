# Bench-side findings (follow-up) — M3 batch d9bf, 2026-05-31

**Status: REPORT ONLY.** Per `solver/CLAUDE.md` the solver side does not modify
the bench. These extend `bench-findings-m3-t2-seekpath-2026-05-29.md`
(Findings 1–4) with three further items found while analysing the v4.3 M3
batch `d9bf61d4` (agent `v4_3`, deepseek-v4-pro, 2026-05-28; T1 avg 0.797 /
T2 avg 0.354). They need the bench owner's decision. Solver-side mitigations
that already landed are noted.

Numbering continues from the earlier doc.

---

## Finding 5 — T1 `Fe_bcc` ground truth is stored in a non-conventional cell (and `point_group` is inconsistent)

**Where:** `bench/questions/M3_T1/M3_T1_Fe_bcc_vc_relax.json` `ground_truth`,
produced by `bench/tools/gt_selfrun/m3/`.

**What:** the Fe_bcc GT lattice is recorded as

| field | GT value | solver (d9bf) | comment |
|-------|----------|---------------|---------|
| a | 2.45254 Å | 2.83120 Å | |
| b | 2.45254 Å | 2.83120 Å | |
| c | 4.00513 Å | 2.83120 Å | |
| α, β | 90° | 90° | |
| γ | **109.483°** | 90° | |
| space_group_number | 229 | 229 | match ✓ |
| point_group | **mmm** | m-3m | inconsistent with sg=229 (cubic → m-3m) |
| crystal_system | cubic | cubic | match ✓ |
| total_magnetization | 4.39 µB | 4.39 | match ✓ |
| absolute_magnetization | 4.64 µB | 4.64 | match ✓ |

The GT is **not** the conventional cubic cell. The solver produced the
textbook conventional bcc-Fe cell (a=b=c=2.831 Å, Im-3m). Converting the GT's
reduced cell to the conventional setting, `a_conv = a_GT / (√3/2) =
2.45254 / 0.8660 = 2.8319 Å`, which **matches the solver's 2.8312 Å**. So the
two describe the *same crystal* — the GT is just in a different (reduced /
primitive-like) cell setting, with a/b unequal to c and γ=109.48°.

Two consequences in the evaluator's raw per-field comparison:
- **a, b, c, γ all score 0** (2.831 vs 2.453 / 4.005; 90° vs 109.48°) — the
  lattice fields are compared verbatim, with no cell canonicalisation.
- **point_group scores 0**: GT `point_group="mmm"` is itself inconsistent with
  `space_group_number=229` / `crystal_system="cubic"` (Im-3m is `m-3m`). The
  solver's correct `m-3m` mismatches the GT's `mmm`.

Net: Fe_bcc loses a+b+c (0.30) + γ (0.05) + point_group (0.05) = **0.40
weight** purely to a cell-convention / extraction artifact, despite producing
physically-correct bcc iron with the right magnetisation. The d9bf score was
0.60 (only the 0.40 magnetisation + α/β/sg/cs survived).

**Why other cubic materials are fine:** Ni_fcc GT is in the conventional cell
(a=3.517 Å, matches the solver) and scored 1.0. The inconsistency is specific
to how the Fe_bcc GT cell was emitted.

**Not solver-fixable without overfitting.** The solver *could* be made to emit
a 2.45/4.00/109.48° cell to match this GT, but that would be fitting to a
malformed reference and would break the moment the GT is regenerated.

**Suggested bench fix (owner's call):**
1. Regenerate the Fe_bcc GT in the standardized conventional cell (consistent
   with Ni_fcc), and fix the `point_group` extraction so it agrees with
   `space_group_number` (229 → `m-3m`); **or**
2. canonicalise both the extracted structure and the GT (e.g. pymatgen
   `SpacegroupAnalyzer.get_conventional_standard_structure`) before the
   per-field lattice comparison, so equivalent cells in different settings
   compare equal.

---

## Finding 6 — refinement of Finding 1: the seekpath crash also nulls METAL fields (smearing-mode path)

**Where:** `bench/harness/lib/evaluator.py:extract_t2_fields` (~696–758).

**What:** Finding 1 stated that true metals are immune to the AFM-split-label
seekpath crash because they hit `_metal_t2_result` before the seekpath block.
That is true **only when the bands NSCF ran with `occupations='fixed'`** (QE
prints the HOMO/LUMO line → `splitter_mode="homo_lumo"` → the metal
short-circuit at line 703 fires *before* seekpath at line 719).

When the bands NSCF ran with **smearing** (no HOMO/LUMO line →
`splitter_mode="fermi"`), the control flow is:

```
L707  else:                       # fermi mode
L715      splitter_mode="fermi"
L719  # seekpath (called HERE, before the fermi-mode metal check) ...
L724      sp_result = seekpath.get_path(sp_input)   # ← crashes on Element("Cr1")
L750  vbm/cbm via fermi ...                          # metal short-circuit L757 — never reached
```

So an **AFM-split metal run with smearing** (e.g. `Cr_bcc`, labels `Cr1`/`Cr2`)
crashes at the seekpath step and gets `band_gap`/`gap_type`/`vbm`/`cbm` all
nulled — exactly like the insulators in Finding 1. In d9bf, Cr_bcc T2 scored
**0.082** (only dos survived), not the ~0.9 a metal should get. The
"metals are immune" note in Finding 1 holds for the fixed-occ path only.

**Solver-side mitigation (FIX-40, S-2, this repo):** for an AFM-split magnetic
metal, the solver now forces `occupations='fixed'` on the bands NSCF, which
moves the run into `homo_lumo` mode where the metal short-circuit precedes
seekpath — so `gap_type=metal` is recovered without the bench ever calling
seekpath on the split labels.

**Suggested bench fix (owner's call):** the same fix proposed for Finding 1
(normalise the species label to its base element before `Element(...)` in
`_build_seekpath_input`) resolves this metal case too — and would let smearing
metal runs score correctly without the solver having to force fixed occ.

---

## Finding 7 — (informational) T1 rubric weights magnetisation 0.35

**Where:** `bench/questions/M3_T1/*.json` `scoring.field_weights`.

**What:** every M3_T1 task weights `total_magnetization_per_cell` = 0.20 and
`absolute_magnetization_per_cell` = 0.15 — **0.35 total**, larger than the
lattice block (a+b+c = 0.30) or the symmetry block (sg+pg+cs = 0.20).

Not a bug. Flagging it because the Finding-3 (FIX-37) score-impact analysis in
the earlier doc accounted only for the symmetry block (sg/pg/cs ≈ 0.20) and did
not mention the magnetisation block. In the d9bf data the magnetisation fields
are in fact the single largest source of T1 score movement: e.g. `Ni_fcc`
(moment collapsed 0→ vs GT 2.67) and `CuO` (came out FM, total=2.0 vs GT AFM
total≈0) each lost the full 0.35 on otherwise-perfect structures (both scored
0.65). The correctness of the **magnetic ground state** — not symmetry — is the
dominant M3_T1 lever; solver work (FIX-33 magnetic-metal moment; the S-1 probe
upgrade) targets it accordingly. No bench action required.

---

## Solver-side status (for context)
- FIX-39 (S-3): per-command pw.x timeout ceiling is now harness-wall-aware
  (0.8 × `SOLVER_HARNESS_WALL_S`, read at call time) instead of a fixed
  12000s, and orphaned mpirun/pw.x is reaped on timeout — addresses the
  Fe3O4/LaMnO3 T2 8h-timeout 0.000s (solver-side, no bench dependency).
- FIX-40 (S-2): AFM-split magnetic metals get `occupations='fixed'` on the
  bands NSCF (Finding 6 work-around).
- Findings 1, 2, 4 (earlier doc) and Finding 5, 6 here remain bench-gated to
  the extent noted; the solver cannot recover `gap_type`/`vbm`/`cbm` for the
  AFM-split insulators without the bench-side label normalisation.

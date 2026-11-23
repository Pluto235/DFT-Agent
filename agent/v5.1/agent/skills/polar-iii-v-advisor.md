---
name: polar-iii-v-advisor
description: Polar III-V semiconductor playbook — wurtzite c/a, LO-TO, band-gap caveats
trigger:
  any_of:
    - material_family: polar_III_V
priority: 10
---

# Key principles

1. Group-13 + group-15 compounds (GaN, AlN, InN, GaAs, GaP, InP, ...) are
   **polar semiconductors**, not metals — insulator-style SCF settings apply
   (no smearing required for the SCF stage, or use a very narrow degauss
   only for numerical stability).
2. Wurtzite-structured III-V (GaN, AlN, InN, ZnO analogue topology) has a
   non-trivial **c/a ratio**. Always allow cell relaxation of both a and c
   independently; constraining to ideal c/a = sqrt(8/3) is a common
   silent-error trap.
3. Bands path must include high-symmetry points relevant to **direct vs
   indirect gap** in the polar zone-center region (Γ, A, M, K, L for
   wurtzite; Γ, X, L for zincblende).
4. LDA/GGA underestimate band gap by a known factor of ~2 for these
   materials. If the task is "report the band gap," check whether the
   problem expects DFT-level or higher-accuracy value.
5. DOS smearing should be tight — these are insulators with sharp band
   edges. Wide DOS smearing washes out the gap.

# Common failure modes

- Cell relax with constrained c/a → relaxed structure looks slightly wrong;
  forces appear converged but stress remains anisotropic.
- Coarse k-mesh on a wurtzite cell → misses the M-K-Γ band features;
  reported gap may be off because the band extrema aren't sampled.
- Confusing zincblende vs wurtzite k-paths → use `seekpath.get_path` with
  the correct relaxed structure; it returns the right path AND the
  standardized primitive cell. Use `path["primitive_lattice"]` /
  `path["primitive_positions"]` / `path["primitive_types"]` as pw.x's
  CELL_PARAMETERS / ATOMIC_POSITIONS / species (NOT the original input
  cell), and write `path["point_coords"]` STRAIGHT into `K_POINTS
  crystal_b`. **DO NOT hand-transform k-points between cell bases**
  (`reciprocal_primitive_lattice` × `inv(cell).T` etc.) — for indirect-gap
  zincblende semiconductors that silently drops the X-point CBM and the
  bands NSCF then reports a fake direct gap at Γ.

# Debug heuristics

- If the relaxed lattice ratio is suspicious, inspect press_conv_thr and
  cell_dofree — locked symmetry constraints are the usual culprit.
- If reported gap is way off from literature DFT values, check whether
  k-path actually visits the conduction-band minimum location.

# Counter-examples (do NOT do this)

- ❌ Wide smearing (Methfessel-Paxton on a polar III-V) — that's metals'
  territory; here it broadens the gap edge artificially.
- ❌ Constraining cell_dofree to isotropic for wurtzite cells — c/a will
  not relax independently from a.
- ❌ Using the zincblende high-symmetry path for a wurtzite material.

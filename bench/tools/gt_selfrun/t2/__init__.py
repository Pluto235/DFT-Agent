"""T2 (bands + DOS) ground-truth pipeline for tools/gt_selfrun.

Each material runs 4 sequential steps:
  1. SCF (high-precision, T1 final geometry)
  2. NSCF (denser kmesh, all bands)
  3. bands.x along seekpath (band-structure post-processing)
  4. dos.x (Gaussian-broadened DOS)

Outputs a schema-v1.2-compatible JSON in canonical_gt_t2/.
"""

---
name: low-symmetry-oxide-advisor
description: Low-symmetry oxide playbook — anisotropic k-mesh, cell_dofree caution
trigger:
  any_of:
    - material_family: oxide
  all_of:
    - crystal_system: monoclinic
priority: 10
---

# Key principles

1. Monoclinic and triclinic oxides (HfO2 baddeleyite, ZrO2 m, TiO2 brookite,
   V2O5, ...) have **anisotropic reciprocal lattices**. k-mesh must reflect
   that — a uniform N×N×N mesh under-samples short reciprocal directions
   and over-samples long ones.
2. Choose k-points per axis **inversely proportional to the real-space
   lattice length**. The Quantum ESPRESSO automatic k-mesh helper or
   pymatgen's KPath utility both honour this; do not pick a uniform mesh
   by hand.
3. vc-relax for low-symmetry cells **must not** clamp cell_dofree to a
   high-symmetry setting. The cell shape is part of the answer — using
   cell_dofree='ibrav' or '2D' freezes the structural degrees of freedom
   that need to relax.
4. Forces in low-symmetry cells often have small components along the
   "off-diagonal" lattice axes — tight forc_conv_thr is needed to detect
   convergence properly.
5. Convergence is **slower** than for cubic cells. Expect more SCF
   iterations per relaxation step and more relaxation steps overall.

# Common failure modes

- Uniform k-mesh → SCF energy converges to the wrong value; differs by
  meV-scale from a properly-anisotropic mesh.
- cell_dofree too restrictive → relaxation reports converged but the
  reported lattice parameters look like the input was not allowed to
  move.
- nstep set low → relaxation appears to "fail" but only because the
  outer loop budget was too small for a low-symmetry geometry.

# Debug heuristics

- If relaxed cell parameters equal input within 4 decimals, suspect
  cell_dofree first.
- If SCF energy depends strongly on k-mesh sampling, check that mesh
  density per reciprocal axis is uniform (in 1/Å), not uniform in N.

# Counter-examples (do NOT do this)

- ❌ Uniform k-mesh on monoclinic/triclinic cells.
- ❌ cell_dofree='ibrav' on a P2_1/c or similar low-symmetry space group.
- ❌ Using a cubic-cell convergence budget (nstep, electron_maxstep)
   without adjustment.

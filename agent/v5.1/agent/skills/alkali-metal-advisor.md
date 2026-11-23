---
name: alkali-metal-advisor
description: Alkali metal SCF/bands playbook — partial occupations, smearing required
trigger:
  any_of:
    - material_family: alkali_metal
priority: 10
---

# Key principles

1. Pure alkali metals are **metallic**. SCF requires fractional occupations
   via smearing — fixed occupations will not converge.
2. Use a smearing scheme appropriate for metallic states (Marzari-Vanderbilt
   "mv" or Methfessel-Paxton "mp" — both are standard). Cold smearing "mv"
   tends to be more robust for the convergence stage.
3. Pick the **lower end** of the typical degauss range. Too wide a smearing
   over-broadens the Fermi surface and biases total energy; too narrow
   destabilises SCF convergence on coarse meshes.
4. k-mesh density must be **higher than for insulators of the same cell**:
   metallic Fermi surface needs adequate sampling, otherwise total energy
   oscillates between SCF steps.
5. Mixing: a moderate mixing_beta is usually safer than the default for
   metals because charge sloshing is more severe with partial occupations.

# Common failure modes

- SCF reports "convergence NOT achieved" within reasonable iterations →
  check the smearing scheme and degauss first, before reaching for nstep.
- Total energy oscillates between consecutive SCF runs → k-mesh likely
  too coarse for the metallic Fermi surface.
- Heavy alkali (Rb, Cs) bands look "wrong" → may need scalar-relativistic
  pseudopotentials; not all pseudo sets support these well.

# Debug heuristics

- If SCF stalls, the order to check is: (a) occupations='smearing', (b)
  degauss in safe range, (c) mixing_beta lowered slightly, (d) k-mesh
  density. Do **not** simply increase electron_maxstep — that masks the
  real problem.
- For bands plotting, dense k-paths near the zone center give cleaner
  Fermi-level crossings than uniform sampling.

# Counter-examples (do NOT do this)

- ❌ occupations='fixed' on a metal — guaranteed SCF failure.
- ❌ Copying an insulator k-mesh density to an alkali metal — Fermi surface
  under-sampling.
- ❌ Setting an enormous nstep to "force" convergence with bad smearing.

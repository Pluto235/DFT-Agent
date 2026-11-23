---
name: complex-silicate-advisor
description: Complex silicate playbook — cell relax first, soft modes, flat-band DOS
trigger:
  any_of:
    - material_family: silicate
priority: 10
---

# Key principles

1. Silicates (Si-O framework + additional cations: alkali, alkaline earth,
   Al, transition metal) have **multiple inequivalent bonding environments**.
   Each cation type contributes its own valence band features.
2. **Always do cell relax before any production SCF.** Initial geometries
   from structural databases are often approximate (averaged over thermal
   ellipsoids), and silicate frameworks deform measurably even at zero
   temperature DFT.
3. Use **tight force convergence** for vc-relax. Silicates have soft
   librational and tilting modes — under-converged forces yield total
   energies that look stable but represent a metastable distortion.
4. For DOS: silicates often have **very flat bands** near the Fermi level
   from cation states (e.g., Al-3p, Li-2s). Flat bands require denser
   k-mesh than typical insulators to resolve cleanly.
5. Plain Si-O frameworks without extra cations (quartz, cristobalite) are
   **not silicates** for the purpose of this advisor — they're better
   treated as simple oxides.

# Common failure modes

- Using database geometry directly for SCF → total energy several meV/atom
   off the relaxed minimum; downstream band gap and DOS are biased.
- Coarse forc_conv_thr → "converged" relaxation that's still on a saddle
   point of a soft mode; small perturbation reveals lower energy.
- Cubic-style k-mesh on a monoclinic silicate (spodumene, jadeite, ...) →
   compounds an anisotropy problem with a soft-mode problem.

# Debug heuristics

- If the relaxed energy seems noisy or oscillates between vc-relax
   restarts, tighten forc_conv_thr and press_conv_thr together.
- If DOS near Fermi level looks "spiky" beyond what the broadening would
   predict, increase k-mesh density before increasing nbnd.

# Counter-examples (do NOT do this)

- ❌ Running SCF directly on a database CIF without vc-relax first.
- ❌ Loose forc_conv_thr on a silicate vc-relax — soft modes will fool
   you into thinking it converged.
- ❌ Identical k-mesh to a cubic reference of similar formula weight —
   silicates are usually low-symmetry and need anisotropic sampling.
